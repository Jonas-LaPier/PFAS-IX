#!/usr/bin/env python3


import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from common import ROOT, input_hashes, manifest


def main():
    p = argparse.ArgumentParser(description=None)
    p.add_argument("batch", choices=["pilot", "a400", "remaining"])
    p.add_argument("--submit", action="store_true")
    p.add_argument(
        "--retry", help="Explicit job ID to rerun after inspecting failed attempts"
    )
    a = p.parse_args()
    cfg = json.loads((ROOT / "sherlock.json").read_text())
    subprocess.run(["python3", str(ROOT / "scripts/validate.py")], check=True)
    rows = [
        r
        for r in manifest()
        if (
            r["pilot"] == "1"
            if a.batch == "pilot"
            else r["batch"] == ("1" if a.batch == "a400" else "2")
        )
    ]
    receipts = ROOT / "results/submissions"
    existing = set()
    if receipts.exists():
        for f in receipts.glob("*/receipt.json"):
            receipt = json.loads(f.read_text())
            existing.update(receipt["jobs"])
    if a.retry:
        rows = [r for r in rows if r["job"] == a.retry]
        if not rows:
            raise SystemExit("Retry job does not belong to selected batch")
    else:
        rows = [r for r in rows if r["job"] not in existing]
    print(
        f"{a.batch}: {len(rows)} workflows; max {cfg['max_parallel']} concurrent per resource group."
    )
    for r in rows:
        print(r["job"], r["cpus"], "CPUs", r["mem_gb"], "GB", r["hours"], "hours")
    if not a.submit:
        print("Preview only. Add --submit on Sherlock to launch.")
        return
    if not rows:
        return
    if a.batch != "pilot" and not a.retry:
        from analyze import collect

        accepted, _ = collect()
        required = [
            r["job"]
            for r in manifest()
            if (r["pilot"] == "1" if a.batch == "a400" else r["batch"] == "1")
        ]
        missing = [job for job in required if job not in accepted]
        if missing:
            raise SystemExit(
                "Previous batch is not validated: "
                + str(len(missing))
                + " required workflows missing/rejected. Run analysis and inspect results first."
            )
    scratch = os.environ.get("SCRATCH")
    if not scratch or not ROOT.is_relative_to(Path(scratch).resolve()):
        raise SystemExit("Submit from a package under Sherlock $SCRATCH.")
    if not 1 <= int(cfg["max_parallel"]) <= 8:
        raise SystemExit("Set max_parallel between 1 and 8.")

    groups = {}
    for r in rows:
        groups.setdefault((r["cpus"], r["mem_gb"], r["hours"]), []).append(r)
    receipts.mkdir(parents=True, exist_ok=True)
    lock = receipts / ".submit_lock"
    with lock.open("x"):
        pass
    try:
        reserved = set()
        for path in receipts.glob("*/receipt.json"):
            reserved.update(json.loads(path.read_text())["jobs"])
        for (cpus, mem, hours), group in groups.items():
            if not a.retry:
                group = [r for r in group if r["job"] not in reserved]
            if not group:
                continue
            folder = receipts / (
                time.strftime("%Y%m%dT%H%M%S") + "_" + str(time.time_ns())
            )
            folder.mkdir()
            jobs = folder / "jobs.txt"
            jobs.write_text("\n".join(r["job"] for r in group) + "\n")
            script = folder / "run.sbatch"
            script.write_text((ROOT / "scripts/run_array.sbatch").read_text())
            config_snapshot = folder / "sherlock.json"
            config_snapshot.write_text(json.dumps(cfg, indent=2) + "\n")
            cmd = [
                "sbatch",
                "--parsable",
                "--job-name=pfas_run2",
                "--nodes=1",
                "--ntasks=1",
                f"--cpus-per-task={cpus}",
                f"--mem={mem}G",
                f"--time={hours}:00:00",
                "--partition=" + cfg["partition"],
                f"--array=0-{len(group) - 1}%{cfg['max_parallel']}",
                f"--output={folder}/%A_%a.out",
                f"--error={folder}/%A_%a.err",
            ]
            for key in ["account", "qos"]:
                if cfg[key]:
                    cmd.append("--" + key + "=" + cfg[key])
            cmd.extend([str(script), str(ROOT), str(jobs), str(config_snapshot)])

            receipt = {
                "jobs": [r["job"] for r in group],
                "command": cmd,
                "status": "submission_pending",
                "input_hashes": {r["job"]: input_hashes(r) for r in group},
            }
            path = folder / "receipt.json"
            path.write_text(json.dumps(receipt, indent=2) + "\n")
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            receipt.update(
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
                status="submitted" if result.returncode == 0 else "submission_failed",
            )
            path.write_text(json.dumps(receipt, indent=2) + "\n")
            if result.returncode:
                raise SystemExit("Submission failed; inspect " + str(path))
            print("Submitted", result.stdout.strip())
    finally:
        lock.unlink()


if __name__ == "__main__":
    main()
