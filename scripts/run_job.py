#!/usr/bin/env python3


import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from check_log import check
from common import ROOT, input_hashes, manifest, sha256, stages


def main():
    p = argparse.ArgumentParser(description=None)
    p.add_argument("job")
    p.add_argument("--receipt", type=Path)
    a = p.parse_args()
    if not os.environ.get("SLURM_JOB_ID"):
        raise SystemExit("Run through Slurm, not on a login node.")
    row = next(r for r in manifest() if r["job"] == a.job)
    if int(os.environ.get("SLURM_CPUS_PER_TASK", "0")) < int(row["cpus"]):
        raise SystemExit("Insufficient allocated CPUs")
    expected = input_hashes(row)
    if a.receipt:
        recorded = json.loads(a.receipt.read_text())["input_hashes"][a.job]
        if expected != recorded:
            raise SystemExit("Inputs changed since submission; refusing to run")
    attempt = (
        os.environ["SLURM_JOB_ID"] + "_" + os.environ.get("SLURM_ARRAY_TASK_ID", "0")
    )
    dest = ROOT / "results" / row["job"] / attempt
    dest.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()

    temp = Path(env.get("L_SCRATCH_JOB", str(dest / "scratch"))) / (
        "gaussian_" + row["job"]
    )
    temp.mkdir(parents=True, exist_ok=True)
    env["GAUSS_SCRDIR"] = str(temp)
    meta = {
        "job": row["job"],
        "attempt": attempt,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "input_hashes": input_hashes(row),
        "gaussian_module": env.get("DFT_GAUSSIAN_MODULE", ""),
        "slurm_job_id": env["SLURM_JOB_ID"],
        "status": "running",
        "stage_results": {},
    }
    meta_path = dest / "run.json"

    def save():
        temporary = meta_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(meta, indent=2) + "\n")
        temporary.replace(meta_path)

    save()
    try:
        for stage in stages(row):
            source = ROOT / row["input_dir"] / (stage + ".gjf")
            shutil.copy2(source, dest / source.name)
            if sha256(dest / source.name) != expected[stage]:
                raise RuntimeError("Input changed while queued/running: " + stage)
            with (
                (dest / source.name).open() as inp,
                (dest / (stage + ".log")).open("w") as out,
            ):
                result = subprocess.run(
                    ["g16"],
                    stdin=inp,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    cwd=dest,
                    env=env,
                )
            checked = check(dest / (stage + ".log"), stage, row)
            meta["stage_results"][stage] = {
                k: v for k, v in checked.items() if k != "geometry"
            }
            save()
            if result.returncode or not checked["valid"]:
                raise RuntimeError(f"{stage} failed: " + "; ".join(checked["issues"]))
            checkpoint = dest / (stage + ".chk")
            if not checkpoint.exists() or not checkpoint.stat().st_size:
                raise RuntimeError(stage + " checkpoint missing")
        meta["status"] = "complete"
        meta["log_hashes"] = {s: sha256(dest / (s + ".log")) for s in stages(row)}

        if shutil.which("formchk"):
            with (dest / "formchk.log").open("w") as out:
                rc = subprocess.run(
                    ["formchk", "sp.chk", "sp.fchk"],
                    cwd=dest,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                ).returncode
            meta["formchk_returncode"] = rc
    except Exception as exc:
        meta["status"] = "failed"
        meta["error"] = str(exc)
        raise
    finally:
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save()


if __name__ == "__main__":
    main()
