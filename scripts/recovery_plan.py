import fcntl
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from analyze import inspect_attempt
from backup import storage_status, verify_snapshot
from check_log import check
from common import (
    ROOT,
    config,
    input_hashes,
    manifest,
    runtime_files,
    sha256,
    stages,
    write_json,
)
from health import ACTIVE, queue_state
from numerics import PROFILE
from provenance import geometry_issues, source_audit
from submit import array_command
from validate import validate

VALIDATION = [
    "free_PFNA_c1__water",
    "PC4P-Me_ClCl_c1_p1__water",
    "PC4P-Me_PFOA_s7_Cl_c2_p2__water",
    "PCy3_PFOA_s6_c1_p1__water",
    "PC4P-Me_PFOA_s7_Cl_c2_p2__unsolvated",
]


def load_plan(path):
    plan = json.loads(path.read_text())
    rows = {r["job"]: r for r in manifest()}
    if plan["profile"] != PROFILE or plan["validation"] != VALIDATION:
        raise RuntimeError("Unexpected validation scope or numerical profile")
    all_jobs = (
        plan["validation"] + plan["warmup"] + plan["remaining"] + plan["completed"]
    )
    if len(all_jobs) != 350 or len(set(all_jobs)) != 350 or set(all_jobs) != set(rows):
        raise RuntimeError("Recovery must preserve exactly 350 unique workflows")
    if len(plan["warmup"]) != 8 or not {"PC4P", "PPh3"} <= {
        rows[j]["resin"] for j in plan["warmup"]
    }:
        raise RuntimeError("Warmup must include eight workflows with PC4P and PPh3")
    if any(rows[j]["kind"] != "ion" for j in plan["completed"]):
        raise RuntimeError("Only audited ion workflows may precede validation")
    for job, descriptor in plan["sources"].items():
        if job not in rows:
            raise RuntimeError("Unknown source workflow")
        if not Path(descriptor["attempt"]).is_relative_to(
            Path(plan["runtime_root"]) / "results"
        ):
            raise RuntimeError("Source attempt outside campaign")
    preservation = Path(plan["preservation"])
    if sha256(preservation) != plan["preservation_sha256"]:
        raise RuntimeError("Preservation record changed")
    record = json.loads(preservation.read_text())
    if record["status"] != "preserved" or record["remaining_allocations"]:
        raise RuntimeError("Old allocations not fully preserved and stopped")
    return plan, rows


def record_attempt(record, job, results):
    candidates = []
    for p in (results / job).glob("*/run.json"):
        meta = json.loads(p.read_text())
        if (
            meta.get("recovery_plan") == record["plan_sha256"]
            and meta.get("package_hashes") == record["package_hashes"]
        ):
            candidates.append(p.parent)
    if len(candidates) != 1:
        raise RuntimeError(job + ": expected one corrected attempt")
    return candidates[0]


def gate(record, rows, results, opt_only=False):
    for job in record["jobs"]:
        folder = record_attempt(record, job, results)
        meta = json.loads((folder / "run.json").read_text())
        if meta.get("status") in ("failed", "interrupted", "held"):
            raise RuntimeError(job + ": failed release gate")
        selected = ["opt"] if opt_only else stages(rows[job])
        if not opt_only and meta.get("status") != "complete":
            raise RuntimeError(job + ": workflow not complete")
        backup = json.loads((folder / "backup.json").read_text())
        if backup["status"] != "verified":
            raise RuntimeError(job + ": backup not verified")
        required = []
        for stage in selected:
            log = folder / (stage + ".log")
            if (
                not meta["stage_results"].get(stage, {}).get("valid")
                or not check(log, stage, rows[job])["valid"]
            ):
                raise RuntimeError(job + ": " + stage + " not validated")
            if meta.get("validated_checkpoints", {}).get(stage) != sha256(
                folder / (stage + ".chk")
            ):
                raise RuntimeError(job + ": checkpoint was not validated")
            if meta.get("executed_input_hashes", {}).get(stage) != sha256(
                folder / (stage + ".gjf")
            ):
                raise RuntimeError(job + ": executed input provenance differs")
            if meta["log_hashes"].get(stage) != sha256(log):
                raise RuntimeError(job + ": stage log changed")
            required += [
                "attempt/" + stage + suffix for suffix in (".log", ".chk", ".gjf")
            ]
        stored = verify_snapshot(backup["manifest"], required)
        for name in required:
            if stored["files"][name] != sha256(folder / name.split("/", 1)[1]):
                raise RuntimeError(job + ": backup differs from accepted stage")
        if opt_only:
            if geometry_issues(
                check(folder / "opt.log", "opt", rows[job])["geometry"], rows[job]
            ):
                raise RuntimeError(job + ": structure requires review")
        else:
            _, issues, _, _, stage_results = inspect_attempt(folder, rows[job])
            if issues or any(not r["valid"] for r in stage_results.values()):
                raise RuntimeError(job + ": final scientific checks failed")


def phase_paths(path, phase):
    return path.parent / (path.stem + "-" + phase + ".json")


def submit_plan(path, phase, execute=False):
    path = path.resolve(strict=True)
    plan, rows = load_plan(path)
    cfg = config()
    phase_jobs = plan[
        {"validation": "validation", "warmup": "warmup", "full": "remaining"}[phase]
    ]
    destination = phase_paths(path, phase)
    if destination.exists():
        print("Existing phase record:", destination)
        return
    health = path.parent / "health.json"
    if health.exists() and json.loads(health.read_text()).get("stopped"):
        raise RuntimeError("Recovery health stop requires review")
    results = Path(plan["runtime_root"]) / "results"
    if phase in ("warmup", "full"):
        previous = json.loads(phase_paths(path, "validation").read_text())
        gate(previous, rows, results)
    if phase == "full":
        previous = json.loads(phase_paths(path, "warmup").read_text())
        gate(previous, rows, results, opt_only=True)
    for job in phase_jobs + plan["completed"]:
        if job in plan["sources"]:
            reusable = source_audit(plan["sources"][job], rows[job])
            if job in plan["completed"] and reusable != stages(rows[job]):
                raise RuntimeError("Completed ion failed reuse audit")
    print(
        phase + ": " + str(len(phase_jobs)) + " existing workflows. Profile " + PROFILE
    )
    if not execute:
        print("Preview only. No scheduler changes.")
        return
    if validate():
        raise RuntimeError("Frozen package validation failed")
    if not ROOT.is_relative_to(Path(os.environ["SCRATCH"]).resolve()):
        raise RuntimeError("Submit from Sherlock shared scratch")
    storage_status(new_start=True)
    subprocess.run(["bash", str(ROOT / "scripts/preflight.sh")], check=True)
    lock = (results / "submissions/recovery.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if destination.exists():
        raise RuntimeError("Recovery phase already exists")
    arrays = set(plan["old_arrays"] + plan["diagnostic_arrays"])
    other_phases = []
    for name in ("validation", "warmup", "full"):
        record = phase_paths(path, name)
        if record.exists():
            saved = json.loads(record.read_text())
            arrays.update(saved["arrays"].values())
            other_phases.append(saved)
    queue = queue_state()
    old_running = [
        q
        for q in queue
        if q["array"] in plan["old_arrays"] + plan["diagnostic_arrays"]
        and q["state"] in ACTIVE - {"PENDING"}
    ]
    if old_running:
        raise RuntimeError("Old allocation still active")
    if any(q["array"] in plan["old_arrays"] and q["state"] == "PENDING" for q in queue):
        raw = subprocess.check_output(
            [
                "squeue",
                "--array",
                "-h",
                "-u",
                os.environ["USER"],
                "-t",
                "PENDING",
                "-o",
                "%F|%Q",
            ],
            text=True,
        )
        if any(
            a in plan["old_arrays"] and int(priority) != 0
            for a, priority in (line.split("|") for line in raw.splitlines())
        ):
            raise RuntimeError("Old replacement array is not held")
    active = [
        q for q in queue if q["array"] in arrays and q["state"] in ACTIVE - {"PENDING"}
    ]
    reserve = len(active)
    if reserve > 8 or (reserve and phase != "full"):
        raise RuntimeError("Wait for prior phase allocations to finish")
    folder = (
        results
        / "submissions"
        / (time.strftime("%Y%m%dT%H%M%S") + "_" + phase + "_" + str(time.time_ns()))
    )
    folder.mkdir()
    frozen = folder / "package"
    frozen.mkdir()
    hashes = {str(p.relative_to(ROOT)): sha256(p) for p in runtime_files()}
    for name in hashes:
        target = frozen / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    (frozen / "checks").mkdir()
    shutil.copy2(ROOT / "checks/SHA256SUMS.json", frozen / "checks/SHA256SUMS.json")
    shutil.copy2(path, folder / "plan.json")
    record = dict(
        status="prepared",
        phase=phase,
        plan_sha256=sha256(path),
        plan=str(path),
        jobs=phase_jobs,
        package=str(frozen),
        package_hashes=hashes,
        arrays={},
        receipts={},
    )
    write_json(destination, record)
    prepared = []
    for group in ("small", "large"):
        members = [
            rows[job] for job in phase_jobs if rows[job]["resource_group"] == group
        ]
        if not members:
            continue
        target = folder / group
        target.mkdir()
        (target / "jobs.txt").write_text("\n".join(r["job"] for r in members) + "\n")
        local_cfg = dict(cfg, group_parallel=dict(cfg["group_parallel"]))
        local_cfg["group_parallel"][group] = (
            len(members)
            if phase != "full"
            else (7 if group == "small" else 57 - reserve)
        )
        cmd = array_command(local_cfg, group, members, target, frozen, results)
        cmd.insert(1, "--hold")
        receipt = dict(
            jobs=[r["job"] for r in members],
            command=cmd,
            status="prepared",
            input_hashes={r["job"]: input_hashes(r, frozen) for r in members},
            package_hashes=hashes,
            health_dir=str(path.parent),
            recovery_plan=sha256(path),
            phase=phase,
            phase_record=str(destination),
            sources={
                r["job"]: plan["sources"][r["job"]]
                for r in members
                if r["job"] in plan["sources"]
            },
        )
        receipt_path = target / "receipt.json"
        write_json(receipt_path, receipt)
        subprocess.run(
            [cmd[0], "--test-only", *cmd[1:]],
            capture_output=True,
            text=True,
            check=True,
        )
        prepared.append((group, receipt_path, receipt))
    for group, receipt_path, receipt in prepared:
        result = subprocess.run(
            receipt["command"], capture_output=True, text=True, check=False
        )
        receipt.update(
            stdout=result.stdout,
            stderr=result.stderr,
            returncode=result.returncode,
            status="submitted" if result.returncode == 0 else "submission_failed",
        )
        write_json(receipt_path, receipt)
        if result.returncode:
            raise RuntimeError(
                "Scheduler submission failed; existing new arrays remain held"
            )
        identifier = result.stdout.strip().split(";")[0]
        if not identifier.isdigit():
            raise RuntimeError("Unexpected scheduler identifier")
        record["arrays"][group] = identifier
        record["receipts"][group] = str(receipt_path)
        write_json(destination, record)
    record["status"] = "submitted_held"
    write_json(destination, record)
    for identifier in record["arrays"].values():
        subprocess.run(["scontrol", "release", identifier], check=True)
    record["status"] = "submitted"
    write_json(destination, record)
    print("Recovery receipt:", destination)


def guard_allocations(receipt_path):
    receipt = json.loads(Path(receipt_path).read_text())
    if not receipt.get("phase_record"):
        return
    phase = json.loads(Path(receipt["phase_record"]).read_text())
    plan_path = Path(phase["plan"])
    plan = json.loads(plan_path.read_text())
    arrays = set(plan["old_arrays"] + plan["diagnostic_arrays"])
    for name in ("validation", "warmup", "full"):
        path = phase_paths(plan_path, name)
        if path.exists():
            arrays.update(json.loads(path.read_text())["arrays"].values())
    output = subprocess.check_output(
        ["squeue", "--array", "-h", "-u", os.environ["USER"], "-o", "%F|%T|%C|%j"],
        text=True,
    )
    active = []
    for line in output.splitlines():
        array, state, cpus, name = line.split("|")
        if state not in ACTIVE - {"PENDING"}:
            continue
        if name.lower().startswith("pfas") and array not in arrays:
            raise RuntimeError("Unregistered active PFAS allocation")
        if array in arrays:
            active.append(int(cpus))
    if len(active) > 64 or sum(active) > 512:
        raise RuntimeError("Campaign allocation ceiling exceeded")


def rebalance_phase(receipt_path):
    receipt = json.loads(Path(receipt_path).read_text())
    if receipt.get("phase") != "full":
        return
    phase = json.loads(Path(receipt["phase_record"]).read_text())
    folder = Path(receipt["health_dir"])
    with (folder / "rebalance.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (folder / "health.json").exists() and json.loads(
            (folder / "health.json").read_text()
        ).get("stopped"):
            return
        guard_allocations(receipt_path)
        plan = Path(phase["plan"])
        warmup = json.loads(phase_paths(plan, "warmup").read_text())
        queue = [q for q in queue_state() if q["state"] in ACTIVE]
        reserve = sum(q["array"] in warmup["arrays"].values() for q in queue)
        small_live = any(q["array"] == phase["arrays"].get("small") for q in queue)
        large = phase["arrays"].get("large")
        limit = 64 - reserve - (7 if small_live else 0)
        if large and 1 <= limit <= 64 and any(q["array"] == large for q in queue):
            subprocess.run(
                [
                    "scontrol",
                    "update",
                    "JobId=" + large,
                    "ArrayTaskThrottle=" + str(limit),
                ],
                check=True,
                timeout=15,
            )
