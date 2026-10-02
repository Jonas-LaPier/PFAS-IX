import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from common import (
    ROOT,
    config,
    input_hashes,
    manifest,
    runtime_files,
    sha256,
    write_json,
)
from health import ACTIVE, queue_state, rebalance
from submit import array_command
from validate import validate


def hold_originals(identifiers):
    for identifier in identifiers:
        result = subprocess.run(
            ["scontrol", "hold", identifier],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
    result = subprocess.run(
        [
            "squeue",
            "--array",
            "--noheader",
            "--user",
            os.environ["USER"],
            "--states=PENDING",
            "--format=%F|%K|%Q",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    pending = {}
    for line in result.stdout.splitlines():
        array, task, priority = line.strip().split("|")
        if array in identifiers and int(priority) != 0:
            raise RuntimeError(
                "An original pending task is not held; replacement is not safe"
            )
        if array in identifiers:
            pending.setdefault(array, []).append(int(task))
    return [
        array + "_[" + ",".join(str(i) for i in sorted(indices)) + "]"
        for array, indices in pending.items()
    ]


def select_rows(previous, queue):
    active = {
        r["job"]
        for r in queue
        if r["array"] in previous.values()
        and r["state"] in ACTIVE
        and r["state"] != "PENDING"
    }
    rows, deferred, resume, known = [], [], {}, set()
    for row in manifest():
        attempts = []
        for path in (ROOT / "results" / row["job"]).glob("*/run.json"):
            data = json.loads(path.read_text())
            if data.get("input_hashes") != input_hashes(row):
                raise RuntimeError("Attempt inputs differ: " + row["job"])
            attempts.append((data.get("started", ""), path, data))
        if not attempts:
            rows.append(row)
            continue
        _, path, data = max(attempts, key=lambda item: item[0])
        if data["slurm_job_id"] in active:
            known.add(data["slurm_job_id"])
        if data.get("status") == "complete":
            if not all(
                data.get("stage_results", {}).get(s, {}).get("valid")
                for s in row["stages"].split(";")
            ):
                raise RuntimeError(
                    "Completed attempt has invalid stages: " + row["job"]
                )
            continue
        resume[row["job"]] = path.parent.name
        if data["slurm_job_id"] in active:
            deferred.append(row)
        else:
            rows.append(row)
    if known != active:
        raise RuntimeError(
            "An active original task has not recorded its workflow yet; retry after startup"
        )
    position = {r["job"]: i for i, r in enumerate(manifest())}
    rows.sort(
        key=lambda r: (
            r["job"] not in resume,
            {"free": 0, "counterion": 1, "complex": 2}.get(r["kind"], 3)
            if r["job"] in resume
            else 0,
            position[r["job"]],
        )
    )
    return rows + deferred, {r["job"] for r in deferred}, resume


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("submission")
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    if Path(args.submission).name != args.submission:
        raise SystemExit("Use the original submission directory name")
    issues = validate()
    if issues:
        raise SystemExit("\n".join(issues))
    cfg = config()
    submissions = ROOT / "results/submissions"
    original = submissions / args.submission
    previous = {}
    for path in original.glob("*/receipt.json"):
        receipt = json.loads(path.read_text())
        identifier = receipt.get("stdout", "").strip().split(";")[0]
        if receipt.get("status") != "submitted" or not identifier.isdigit():
            raise SystemExit("Original submission is incomplete")
        previous[path.parent.name] = identifier
    if set(previous) != {"ions", "small", "large"}:
        raise SystemExit("Expected the original three campaign arrays")
    queue = queue_state()
    rows, deferred, resume = select_rows(previous, queue)
    if any(row["kind"] == "ion" for row in rows):
        raise SystemExit("Ion references must be complete before this recovery")
    print(
        f"Recovery: {len(rows)} workflows; {len(resume)} saved attempts; {len(deferred)} wait for original running work."
    )
    if not args.submit:
        print("Preview only. No scheduler changes.")
        return
    scratch = os.environ.get("SCRATCH")
    if not scratch or not ROOT.is_relative_to(Path(scratch).resolve()):
        raise SystemExit("Recover from the package under Sherlock $SCRATCH")
    subprocess.run(["bash", str(ROOT / "scripts/preflight.sh")], check=True)
    lock = submissions / ".submit_lock"
    with lock.open("x"):
        pass
    try:
        live = {r["array"] for r in queue_state() if r["state"] in ACTIVE}
        for path in submissions.glob("**/receipt.json"):
            receipt = json.loads(path.read_text())
            identifier = receipt.get("stdout", "").strip().split(";")[0]
            if identifier in live and identifier not in previous.values():
                raise SystemExit("Another PFAS campaign allocation is active")
        if previous["ions"] in live:
            raise SystemExit("Wait for the original ion array to finish")
        original_live = [
            identifier for identifier in previous.values() if identifier in live
        ]
        if original_live:
            pending = hold_originals(original_live)
            if pending:
                subprocess.run(
                    [
                        "scancel",
                        "--state=PENDING",
                        "--user=" + os.environ["USER"],
                        *pending,
                    ],
                    check=True,
                )
        queue = queue_state()
        if any(
            r["array"] in previous.values() and r["state"] == "PENDING" for r in queue
        ):
            raise RuntimeError("Original pending tasks remain; replacement is not safe")
        if (
            sum(r["array"] in previous.values() and r["state"] in ACTIVE for r in queue)
            > cfg["max_parallel"]
        ):
            raise RuntimeError("Original running tasks exceed the recovery budget")
        rows, deferred, resume = select_rows(previous, queue)
        folder = submissions / (
            time.strftime("%Y%m%dT%H%M%S") + "_recovery_" + str(time.time_ns())
        )
        folder.mkdir()
        snapshot = folder / "package"
        snapshot.mkdir()
        hashes = {str(p.relative_to(ROOT)): sha256(p) for p in runtime_files()}
        for name in hashes:
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        (snapshot / "checks").mkdir()
        shutil.copy2(
            ROOT / "checks/SHA256SUMS.json", snapshot / "checks/SHA256SUMS.json"
        )
        if validate(snapshot):
            raise RuntimeError("Recovery snapshot is invalid")
        record = {
            "previous_submission": args.submission,
            "previous_arrays": previous,
            "original_running": queue,
            "jobs": [r["job"] for r in rows],
            "deferred_jobs": sorted(deferred),
            "arrays": {},
            "status": "prepared",
        }
        write_json(folder / "recovery.json", record)
        prepared = []
        for group in ["small", "large"]:
            members = [r for r in rows if r["resource_group"] == group]
            if not members:
                continue
            target = folder / group
            target.mkdir()
            jobs = [r["job"] for r in members]
            (target / "jobs.txt").write_text("\n".join(jobs) + "\n")
            local_cfg = dict(cfg, group_parallel=dict(cfg["group_parallel"]))
            local_cfg["group_parallel"][group] = 1
            cmd = array_command(
                local_cfg, group, members, target, snapshot, ROOT / "results"
            )
            cmd.insert(1, "--hold")
            active = [
                r["job"]
                for r in queue
                if r["array"] in previous.values() and r["state"] in ACTIVE
            ]
            if active:
                cmd.insert(
                    1, "--dependency=" + "?".join("afterany:" + job for job in active)
                )
            receipt = {
                "jobs": jobs,
                "command": cmd,
                "status": "submission_failed",
                "input_hashes": {r["job"]: input_hashes(r, snapshot) for r in members},
                "package_hashes": hashes,
                "health_dir": str(folder),
                "resume": {job: resume[job] for job in jobs if job in resume},
                "restart_opt": True,
                "previous_arrays": previous,
            }
            path = target / "receipt.json"
            write_json(path, receipt)
            subprocess.run(
                [cmd[0], "--test-only", *cmd[1:]],
                capture_output=True,
                text=True,
                check=True,
            )
            prepared.append((group, path, receipt))
        for group, path, receipt in prepared:
            receipt["status"] = "submission_pending"
            write_json(path, receipt)
            result = subprocess.run(
                receipt["command"], capture_output=True, text=True, check=False
            )
            receipt.update(
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
                status="submitted" if not result.returncode else "submission_failed",
            )
            write_json(path, receipt)
            if result.returncode:
                raise RuntimeError("Recovery submission failed: " + result.stderr)
            identifier = result.stdout.strip().split(";")[0]
            if not identifier.isdigit():
                raise RuntimeError("Unexpected scheduler submission identifier")
            record["arrays"][group] = identifier
            write_json(folder / "recovery.json", record)
            count = sum(job in deferred for job in receipt["jobs"])
            if count:
                start = len(receipt["jobs"]) - count
                selection = f"{identifier}_[{start}-{len(receipt['jobs']) - 1}]"
                subprocess.run(
                    [
                        "scontrol",
                        "update",
                        "JobId=" + selection,
                        "Dependency=afterany:" + previous[group],
                    ],
                    check=True,
                )
        record["status"] = "submitted_held"
        write_json(folder / "recovery.json", record)
        write_json(
            folder / "rebalance.json",
            {
                "small": 1,
                "large": 0 if "small" in record["arrays"] else 1,
                "large_released": "small" not in record["arrays"],
            },
        )
        for group, path, receipt in prepared:
            identifier = record["arrays"][group]
            if group == "small" or "small" not in record["arrays"]:
                subprocess.run(["scontrol", "release", identifier], check=True)
            print("Submitted recovery", group, identifier)
        if prepared:
            rebalance(prepared[0][1])
        record["status"] = "submitted"
        write_json(folder / "recovery.json", record)
        print("Recovery record:", folder / "recovery.json")
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
