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
from validate import validate


def reserved_jobs(receipts):
    jobs = set()
    for path in receipts.glob("**/receipt.json"):
        r = json.loads(path.read_text())
        if r["status"] != "submission_failed":
            jobs.update(r["jobs"])
    return jobs


def array_command(cfg, group, rows, folder, snapshot, results):
    row = rows[0]
    cmd = [
        "sbatch",
        "--parsable",
        "--job-name=pfas_ix",
        "--nodes=1",
        "--ntasks=1",
        "--cpus-per-task=" + row["cpus"],
        "--mem=" + row["mem_gb"] + "G",
        f"--time={cfg['group_hours'][group]}:00:00",
        "--partition=" + cfg["partition"],
        f"--array=0-{len(rows) - 1}%{cfg['group_parallel'][group]}",
        f"--output={folder}/%A_%a.out",
        f"--error={folder}/%A_%a.err",
    ]
    for key in ["account", "qos"]:
        if cfg[key]:
            cmd.append("--" + key + "=" + cfg[key])
    cmd.extend(
        [
            str(snapshot / "scripts/run_array.sbatch"),
            str(snapshot),
            str(folder / "jobs.txt"),
            str(results),
        ]
    )
    return cmd


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--submit", action="store_true")
    p.add_argument("--resume", nargs=2, metavar=("JOB", "ATTEMPT"))
    p.add_argument("--restart-opt", action="store_true")
    p.add_argument("--retry-unstarted", action="store_true")
    a = p.parse_args()
    if a.retry_unstarted and a.resume:
        raise SystemExit("Choose either --retry-unstarted or --resume")
    if a.restart_opt and not a.resume:
        raise SystemExit("--restart-opt requires --resume JOB ATTEMPT")
    issues = validate()
    if issues:
        raise SystemExit("\n".join(issues))
    cfg = config()
    rows = manifest()
    receipts = ROOT / "results/submissions"
    if a.resume:
        job, attempt = a.resume
        if Path(attempt).name != attempt or Path(job).name != job:
            raise SystemExit("Use job and attempt directory names")
        old = ROOT / "results" / job / attempt
        oldmeta = json.loads((old / "run.json").read_text())
        rows = [r for r in rows if r["job"] == job]
        if not rows or oldmeta.get("input_hashes") != input_hashes(rows[0]):
            raise SystemExit("Resume inputs differ")
        if oldmeta.get("status") == "complete":
            raise SystemExit("Attempt already complete")
        if a.restart_opt and (
            rows[0]["kind"] == "ion" or not (old / "opt.chk").is_file()
        ):
            raise SystemExit("Optimization checkpoint missing")
    else:
        reserved = reserved_jobs(receipts)
        rows = [
            r
            for r in rows
            if r["job"] not in reserved
            or (
                a.retry_unstarted
                and not list((ROOT / "results" / r["job"]).glob("*/run.json"))
            )
        ]
    groups = {
        g: [r for r in rows if r["resource_group"] == g] for g in cfg["group_parallel"]
    }
    print(
        f"Campaign: {len(rows)} workflows; at most {cfg['max_parallel']} concurrent tasks per submission."
    )
    for g, members in groups.items():
        if members:
            r = members[0]
            print(
                f"{g}: {len(members)} workflows; {r['cpus']} CPUs, {r['mem_gb']} GB, {cfg['group_hours'][g]} hours, concurrency {cfg['group_parallel'][g]}."
            )
    if not a.submit:
        print("Preview only. No jobs submitted.")
        return
    if not rows:
        return
    scratch = os.environ.get("SCRATCH")
    if not scratch or not ROOT.is_relative_to(Path(scratch).resolve()):
        raise SystemExit("Submit from a package under Sherlock $SCRATCH.")
    subprocess.run(["bash", str(ROOT / "scripts/preflight.sh")], check=True)
    receipts.mkdir(parents=True, exist_ok=True)
    lock = receipts / ".submit_lock"
    with lock.open("x"):
        pass
    try:
        live = subprocess.run(
            ["squeue", "--noheader", "--user", os.environ["USER"], "--format=%F %A"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if a.resume and oldmeta["slurm_job_id"].split("_")[0] in live:
            raise SystemExit("Original allocation is still active")
        for path in receipts.glob("**/receipt.json"):
            prior = json.loads(path.read_text())
            if prior.get("stdout", "").strip().split(";")[0] in live:
                raise SystemExit(
                    "A previous campaign allocation is still active; wait before submitting another campaign."
                )
        if not a.resume:
            reserved = reserved_jobs(receipts)
            rows = [
                r
                for r in rows
                if r["job"] not in reserved
                or (
                    a.retry_unstarted
                    and not list((ROOT / "results" / r["job"]).glob("*/run.json"))
                )
            ]
        if not rows:
            return
        folder = receipts / (time.strftime("%Y%m%dT%H%M%S") + "_" + str(time.time_ns()))
        folder.mkdir()
        snapshot = folder / "package"
        snapshot.mkdir()
        hashes = {str(f.relative_to(ROOT)): sha256(f) for f in runtime_files()}
        for name in hashes:
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        (snapshot / "checks").mkdir()
        shutil.copy2(
            ROOT / "checks/SHA256SUMS.json", snapshot / "checks/SHA256SUMS.json"
        )
        if validate(snapshot):
            raise RuntimeError("Snapshot validation failed")
        prepared = []
        for group in cfg["group_parallel"]:
            members = [r for r in rows if r["resource_group"] == group]
            if not members:
                continue
            target = folder / group
            target.mkdir()
            (target / "jobs.txt").write_text(
                "\n".join(r["job"] for r in members) + "\n"
            )
            cmd = array_command(cfg, group, members, target, snapshot, ROOT / "results")
            receipt = {
                "jobs": [r["job"] for r in members],
                "command": cmd,
                "status": "submission_failed",
                "input_hashes": {r["job"]: input_hashes(r, snapshot) for r in members},
                "package_hashes": hashes,
                "health_dir": str(folder),
                "resume": {a.resume[0]: a.resume[1]} if a.resume else {},
                "restart_opt": bool(a.restart_opt),
            }
            path = target / "receipt.json"
            write_json(path, receipt)
            result = subprocess.run(
                [cmd[0], "--test-only", *cmd[1:]],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode:
                raise SystemExit(
                    "Scheduler rejected requested resources: " + result.stderr
                )
            prepared.append((path, receipt))
        for path, receipt in prepared:
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
                raise SystemExit("Submission failed; inspect " + str(path))
            print("Submitted", result.stdout.strip())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
