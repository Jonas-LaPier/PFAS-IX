import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from backup import snapshot
from check_log import check
from common import ROOT, input_hashes, manifest, sha256, stages, write_json
from health import rebalance, update
from numerics import controls, failure_kind, geometry_restart


def restart_input(source, checkpoint, dest, row):
    saved = dest / "recovery.chk"
    shutil.copy2(checkpoint, saved)
    formatted = dest / "recovery.fchk"
    with (dest / "recovery_formchk.log").open("w") as log:
        result = subprocess.run(
            ["formchk", str(saved), str(formatted)],
            stdout=log,
            stderr=subprocess.STDOUT,
            check=False,
        )
    if result.returncode or not formatted.exists():
        raise RuntimeError(
            "Partial checkpoint is unreadable; restart optimization from its original input"
        )
    text = formatted.read_text()
    for name, expected in [
        ("Number of atoms", int(row["atoms"])),
        ("Charge", int(row["charge"])),
        ("Multiplicity", 1),
    ]:
        found = re.search(r"^" + name + r"\s+I\s+(-?\d+)\s*$", text, re.MULTILINE)
        if not found or int(found[1]) != expected:
            raise RuntimeError("Partial checkpoint " + name + " differs")
    match = re.search(
        r"^Atomic numbers\s+I\s+N=\s*(\d+)\s*\n(.*?)(?=^[A-Za-z]|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    numbers = [int(n) for n in match[2].split()] if match else []
    structure = json.loads(
        (ROOT / "structures" / (row["structure"] + ".cjson")).read_text()
    )
    if numbers != structure["atoms"]["elements"]["number"]:
        raise RuntimeError("Partial checkpoint atom order differs")
    original = source.read_text()
    prefix, _body, basis = original.split("\n\n", 2)
    prefix = prefix.replace("%Chk=opt.chk", "%OldChk=recovery.chk\n%Chk=opt.chk")
    prefix += " Geom=AllCheck Guess=Read"
    basis = basis.split("\n\n", 1)[1]
    target = dest / "opt.gjf"
    target.write_text(prefix + "\n\n" + basis)
    return {
        "checkpoint_sha256": sha256(saved),
        "input_sha256": sha256(target),
        "source": str(checkpoint),
        "mode": "New optimization from last checkpoint geometry; fresh Hessian",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("job")
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--results-root", type=Path, required=True)
    a = p.parse_args()
    if not os.environ.get("SLURM_JOB_ID"):
        raise SystemExit("Run through Slurm, not on a login node.")
    receipt = json.loads(a.receipt.read_text())
    row = next(r for r in manifest() if r["job"] == a.job)
    if int(os.environ.get("SLURM_CPUS_PER_TASK", "0")) < int(row["cpus"]):
        raise SystemExit("Insufficient allocated CPUs")
    expected = input_hashes(row)
    if expected != receipt["input_hashes"][a.job]:
        raise SystemExit("Submission input mismatch")
    attempt = (
        os.environ["SLURM_JOB_ID"] + "_" + os.environ.get("SLURM_ARRAY_TASK_ID", "0")
    )
    dest = a.results_root / a.job / attempt
    dest.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    scratch = env.get("L_SCRATCH_JOB")
    if not scratch:
        raise SystemExit("Sherlock job-local scratch is unavailable")
    temp = Path(scratch) / ("gaussian_" + a.job)
    temp.mkdir(parents=True, exist_ok=True)
    env["GAUSS_SCRDIR"] = str(temp)
    meta = {
        "job": a.job,
        "attempt": attempt,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "input_hashes": expected,
        "package_hashes": receipt["package_hashes"],
        "gaussian_module": env.get("DFT_GAUSSIAN_MODULE", ""),
        "slurm_job_id": env["SLURM_JOB_ID"],
        "status": "running",
        "stage_results": {},
        "log_hashes": {},
        "checkpoint_hashes": {},
    }

    def save():
        tmp = dest / "run.tmp"
        tmp.write_text(json.dumps(meta, indent=2) + "\n")
        tmp.replace(dest / "run.json")

    def protect():
        try:
            record = snapshot(dest, a.receipt)
            write_json(dest / "backup.json", {"status": "verified", "manifest": record})
        except (OSError, ValueError, RuntimeError) as exc:
            write_json(dest / "backup.json", {"status": "failed", "error": str(exc)})
            update(a.receipt, a.job, "Backup failed: " + str(exc))
            raise RuntimeError(
                "Backup failed; original outputs retained: " + str(exc)
            ) from exc

    save()
    resume = receipt.get("resume", {}).get(a.job)

    def interrupted(signum, frame):
        raise InterruptedError("Allocation interrupted by signal " + str(signum))

    signal.signal(signal.SIGTERM, interrupted)
    try:
        try:
            rebalance(a.receipt)
        except (OSError, ValueError, KeyError) as exc:
            print("Concurrency update unavailable:", exc)
        reusable = True
        old = None
        oldmeta = {}
        if resume:
            old = a.results_root / a.job / resume
            oldmeta = json.loads((old / "run.json").read_text())
            if oldmeta.get("input_hashes") != expected:
                raise RuntimeError("Cannot resume different inputs")
        for stage in stages(row):
            source = ROOT / row["input_dir"] / (stage + ".gjf")
            shutil.copy2(source, dest / source.name)
            if sha256(dest / source.name) != expected[stage]:
                raise RuntimeError("Input changed: " + stage)
            log = dest / (stage + ".log")
            checkpoint = dest / (stage + ".chk")
            reuse = False
            if old and reusable:
                oldlog = old / (stage + ".log")
                oldchk = old / (stage + ".chk")
                if (
                    oldlog.exists()
                    and oldchk.exists()
                    and check(oldlog, stage, row)["valid"]
                    and oldmeta.get("log_hashes", {}).get(stage) == sha256(oldlog)
                    and oldmeta.get("checkpoint_hashes", {}).get(stage)
                    == sha256(oldchk)
                ):
                    shutil.copy2(oldlog, log)
                    shutil.copy2(oldchk, checkpoint)
                    shutil.copy2(old / source.name, dest / source.name)
                    reuse = True
                if not reuse:
                    reusable = False
            if update(a.receipt, a.job):
                meta["status"] = "held"
                raise RuntimeError(
                    "Campaign stopped after repeated setup or parser failures"
                )
            if stage == "opt" and old and not reuse and receipt.get("restart_opt"):
                previous_text = (old / "opt.log").read_text(errors="replace")
                previous_failure = failure_kind(previous_text)
                if previous_failure in ["scf", "pcm"]:
                    meta["optimization_restart"] = geometry_restart(
                        source, old / "opt.log", dest, row
                    )
                elif (old / "opt.chk").is_file():
                    meta["optimization_restart"] = restart_input(
                        source, old / "opt.chk", dest, row
                    )
                else:
                    raise RuntimeError("Optimization checkpoint missing")
            else:
                previous_failure = None
            meta["current_stage"] = stage
            save()
            if reuse:
                meta.setdefault("reused_stages", {})[stage] = str(old)
                if stage in oldmeta.get("numerical_controls", {}):
                    meta.setdefault("numerical_controls", {})[stage] = oldmeta[
                        "numerical_controls"
                    ][stage]
                rc = 0
            else:
                meta.setdefault("numerical_controls", {})[stage] = controls(
                    dest / source.name, stage, previous_failure
                )
                save()
                while True:
                    with (dest / source.name).open() as inp, log.open("w") as out:
                        rc = subprocess.run(
                            ["g16"],
                            stdin=inp,
                            stdout=out,
                            stderr=subprocess.STDOUT,
                            cwd=dest,
                            env=env,
                            check=False,
                        ).returncode
                    failed_kind = failure_kind(log.read_text(errors="replace"))
                    if (
                        stage != "opt"
                        or check(log, stage, row)["valid"]
                        or failed_kind not in ["scf", "pcm"]
                        or previous_failure in ["scf", "pcm"]
                    ):
                        break
                    history = {
                        "failure": failed_kind,
                        "log_sha256": sha256(log),
                        "input_sha256": sha256(dest / source.name),
                    }
                    meta["optimization_recovery"] = history
                    save()
                    protect()
                    for suffix in ["gjf", "log", "chk"]:
                        original = dest / ("opt." + suffix)
                        if original.exists():
                            original.rename(dest / ("opt-before-recovery." + suffix))
                    meta["optimization_restart"] = geometry_restart(
                        source, dest / "opt-before-recovery.log", dest, row
                    )
                    previous_failure = failed_kind
                    meta["numerical_controls"][stage] = controls(
                        dest / source.name, stage, failed_kind
                    )
                    save()
            meta.setdefault("executed_input_hashes", {})[stage] = sha256(
                dest / source.name
            )
            checked = check(log, stage, row)
            meta["stage_results"][stage] = {
                k: v for k, v in checked.items() if k != "geometry"
            }
            meta["log_hashes"][stage] = sha256(log)
            if checkpoint.exists():
                meta["checkpoint_hashes"][stage] = sha256(checkpoint)
            save()
            protect()
            if rc or not checked["valid"]:
                text = log.read_text(errors="replace")
                if (
                    not rc
                    and "Normal termination of Gaussian" in text
                    and checked["issues"]
                    != ["Imaginary frequencies; minimum not accepted"]
                ) or any(
                    x in text
                    for x in [
                        "QPErr",
                        "Error opening",
                        "Permission denied",
                        "command not found",
                    ]
                ):
                    update(
                        a.receipt, a.job, stage + ": " + "; ".join(checked["issues"])
                    )
                raise RuntimeError(stage + ": " + "; ".join(checked["issues"]))
            if not checkpoint.exists() or not checkpoint.stat().st_size:
                raise RuntimeError(stage + " checkpoint missing")
        meta["status"] = "complete"
        if shutil.which("formchk"):
            with (dest / "formchk.log").open("w") as out:
                meta["formchk_returncode"] = subprocess.run(
                    ["formchk", "sp.chk", "sp.fchk"],
                    cwd=dest,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    check=False,
                ).returncode
    except Exception as exc:
        if meta["status"] != "held":
            meta["status"] = (
                "interrupted" if isinstance(exc, InterruptedError) else "failed"
            )
        if isinstance(exc, (FileNotFoundError, PermissionError)):
            update(a.receipt, a.job, str(exc))
        meta["error"] = str(exc)
        raise
    finally:
        for checkpoint in dest.glob("*.chk"):
            if checkpoint.stat().st_size:
                meta["checkpoint_hashes"][checkpoint.stem] = sha256(checkpoint)
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save()
        protect()
        try:
            rebalance(a.receipt)
        except (OSError, ValueError, KeyError) as exc:
            print("Concurrency update unavailable:", exc)


if __name__ == "__main__":
    main()
