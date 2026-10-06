import argparse
import fcntl
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from backup import snapshot, storage_status
from check_log import check
from common import ROOT, config, input_hashes, manifest, sha256, stages, write_json
from health import update, rebalance
from numerics import (
    PROFILE,
    STEP_LIMIT,
    allocated_steps,
    optimization_limit,
    controls,
    failure_kind,
    geometry_restart,
    step_count,
)
from provenance import checkpoint_readable, geometry_issues, input_model, source_audit
from analyze import same_geometry
from supervisor import Supervisor, InterruptedCalculation, telemetry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    args = parser.parse_args()
    if not os.environ.get("SLURM_JOB_ID"):
        raise SystemExit("Run through Slurm, not on a login node")
    receipt = json.loads(args.receipt.read_text())
    row = next(r for r in manifest() if r["job"] == args.job)
    expected = input_hashes(row)
    if expected != receipt["input_hashes"][args.job]:
        update(args.receipt, args.job, "Submission inputs differ", "provenance")
        raise SystemExit("Submission inputs differ")
    if int(os.environ.get("SLURM_CPUS_PER_TASK", "0")) < int(row["cpus"]):
        raise SystemExit("Insufficient allocated CPUs")
    if update(args.receipt, args.job):
        raise SystemExit("Campaign health stop blocks new work")
    parent = args.results_root / args.job
    parent.mkdir(parents=True, exist_ok=True)
    lock = (parent / "workflow.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        update(args.receipt, args.job, "Duplicate active workflow", "provenance")
        raise SystemExit("Duplicate active workflow")
    attempt = (
        os.environ["SLURM_JOB_ID"] + "_" + os.environ.get("SLURM_ARRAY_TASK_ID", "0")
    )
    dest = parent / attempt
    dest.mkdir(exist_ok=False)
    meta = dict(
        job=args.job,
        attempt=attempt,
        started=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        input_hashes=expected,
        package_hashes=receipt["package_hashes"],
        gaussian_module=os.environ.get("DFT_GAUSSIAN_MODULE", ""),
        slurm_job_id=os.environ["SLURM_JOB_ID"],
        status="running",
        stage_results={},
        log_hashes={},
        checkpoint_hashes={},
        executed_input_hashes={},
        numerical_controls={},
        numerical_profile=PROFILE,
        recovery_plan=receipt.get("recovery_plan"),
        optimization_steps=0,
        recovery_segments=[],
        retry_used=False,
    )
    supervisor = Supervisor(config().get("progress_snapshot_hours", 12) * 3600)
    supervisor.install()
    category = "setup"
    scratch = None
    backup_safe = True
    exitcode = 1

    def save():
        write_json(dest / "run.json", meta)

    def protect(reason="stage completion"):
        meta["snapshot_reason"] = reason
        meta["interrupted_checkpoints"] = reason != "stage completion"
        save()
        try:
            record = snapshot(dest, args.receipt)
            write_json(
                dest / "backup.json",
                dict(
                    status="verified",
                    manifest=record,
                    manifest_sha256=sha256(Path(record)),
                    reason=reason,
                ),
            )
        except Exception as exc:
            write_json(dest / "backup.json", dict(status="failed", error=str(exc)))
            update(args.receipt, args.job, "Backup failed: " + str(exc), "backup")
            raise

    save()
    try:
        category = "backup"
        meta["storage"] = storage_status(new_start=True)
        from recovery_plan import guard_allocations

        guard_allocations(args.receipt)
        rebalance(args.receipt)
        category = "file_access"
        scratch_base = (
            Path(os.environ["SCRATCH"]).resolve(strict=True) / "PFAS-IX-gaussian"
        )
        scratch_base.mkdir(exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=attempt + "-", dir=scratch_base))
        env = dict(os.environ, GAUSS_SCRDIR=str(scratch))
        meta["environment"] = telemetry(scratch)
        save()
        category = "provenance"
        descriptor = receipt.get("sources", {}).get(args.job)
        reusable = source_audit(descriptor, row) if descriptor else []
        old = Path(descriptor["attempt"]) if descriptor else None
        if descriptor:
            meta["source"] = descriptor
            meta["audited_reusable_stages"] = reusable
        continuation = receipt.get("continuations", {}).get(args.job)
        if continuation:
            prior = Path(continuation["attempt"])
            if sha256(prior / "run.json") != continuation["meta_sha256"]:
                raise RuntimeError("Continuation source changed")
            previous = json.loads((prior / "run.json").read_text())
            if (
                previous["status"] not in ("failed", "interrupted")
                or previous["job"] != args.job
            ):
                raise RuntimeError(
                    "Continuation requires a terminal attempt of this workflow"
                )
            failed_log = prior / "opt-before-recovery.log"
            evidence_path = Path(receipt["numerical_evidence"])
            if (
                previous["optimization_steps"] != 0
                or len(previous["recovery_segments"]) != 1
                or sha256(failed_log) != previous["recovery_segments"][0]["log_sha256"]
                or failure_kind(failed_log.read_text(errors="replace")) != "pcm"
                or sha256(evidence_path) != receipt["numerical_evidence_sha256"]
                or json.loads(evidence_path.read_text())["status"] != "passed"
            ):
                raise RuntimeError("PCM continuation evidence differs")
            meta["optimization_steps"] = previous["optimization_steps"]
            meta["recovery_segments"] = previous["recovery_segments"]
            meta["retry_used"] = True
            meta["recovery_cause"] = "pcm"
            meta["continuation"] = continuation
        optimized = None
        for stage in stages(row):
            source = ROOT / row["input_dir"] / (stage + ".gjf")
            target, log, checkpoint = [
                dest / (stage + suffix) for suffix in (".gjf", ".log", ".chk")
            ]
            shutil.copy2(source, target)
            reuse = stage in reusable
            if reuse:
                for suffix in (".gjf", ".log", ".chk"):
                    shutil.copy2(old / (stage + suffix), dest / (stage + suffix))
                try:
                    checkpoint_readable(
                        checkpoint,
                        row,
                        dest,
                        expected_geometry=check(log, stage, row)["geometry"],
                    )
                except RuntimeError as exc:
                    reuse = False
                    reusable = []
                    meta.setdefault("rejected_checkpoint", {})[stage] = str(exc)
                    for suffix in (".gjf", ".log", ".chk"):
                        (dest / (stage + suffix)).rename(
                            dest / (stage + "-unreadable" + suffix)
                        )
                    shutil.copy2(source, target)
            if stage == "opt" and old and not reuse and (old / "opt.log").exists():
                meta["optimization_restart"] = geometry_restart(
                    source, old / "opt.log", dest, row
                )
            meta["current_stage"] = stage
            save()
            rc = 0
            if reuse:
                meta.setdefault("reused_stages", {})[stage] = str(old)
            else:
                recovery = "pcm" if continuation and stage == "opt" else None
                while True:
                    remaining = STEP_LIMIT - meta["optimization_steps"]
                    numerical = controls(
                        target,
                        stage,
                        recovery,
                        row,
                        remaining,
                        pcm_solver=receipt.get("pcm_solver", {}).get(args.job)
                        == "iterative"
                        or meta.get("recovery_cause") == "pcm",
                    )
                    if input_model(target.read_text(), stage) != input_model(
                        source.read_text(), stage
                    ):
                        category = "provenance"
                        raise RuntimeError(
                            "Executed scientific model differs from frozen input"
                        )
                    meta["numerical_controls"][stage] = numerical
                    meta["executed_input_hashes"][stage] = sha256(target)
                    save()
                    category = "unknown"
                    verified_allocation = False
                    last_observed = 0

                    def observe():
                        nonlocal verified_allocation, last_observed, category
                        if stage == "opt" and not verified_allocation:
                            with log.open(errors="replace") as stream:
                                printed = allocated_steps(stream.read(2000000))
                            if printed is not None:
                                meta["actual_optimization_allocation"] = printed
                                effective = optimization_limit(
                                    log.read_text(errors="replace")
                                )
                                meta["effective_optimization_limit"] = effective
                                if printed < remaining or (
                                    effective is not None and effective != remaining
                                ):
                                    category = "provenance"
                                    raise RuntimeError(
                                        "Gaussian allocated "
                                        + str(printed)
                                        + " steps; expected "
                                        + str(remaining)
                                    )
                                verified_allocation = effective is not None
                                save()
                        if time.monotonic() - last_observed >= 300:
                            meta["environment"] = telemetry(scratch)
                            meta["gaussian_process_group"] = supervisor.child.pid
                            save()
                            last_observed = time.monotonic()

                    with target.open() as inp, log.open("w") as out:
                        rc = supervisor.run(
                            ["g16"], inp, out, dest, env, protect, observe
                        )
                    text = log.read_text(errors="replace")
                    if stage == "opt":
                        count = step_count(text)
                        printed = allocated_steps(text)
                        effective = optimization_limit(text)
                        meta["optimization_steps"] += count
                        meta["recovery_segments"].append(
                            dict(
                                steps=count,
                                allocated=printed,
                                effective_limit=effective,
                                requested=remaining,
                                input_sha256=sha256(target),
                                log_sha256=sha256(log),
                            )
                        )
                        if (
                            count
                            and (
                                printed is None
                                or printed < remaining
                                or effective != remaining
                            )
                        ) or count > remaining:
                            category = "provenance"
                            raise RuntimeError(
                                "Printed optimization allocation or total budget differs"
                            )
                    checked = check(log, stage, row)
                    failure = failure_kind(text, rc)
                    if not rc and checked["valid"]:
                        break
                    retry_allowed = not meta["retry_used"] and (
                        failure == "scf" or (failure == "pcm" and stage == "opt")
                    )
                    if stage == "opt" and meta["optimization_steps"] >= STEP_LIMIT:
                        retry_allowed = False
                    if not retry_allowed:
                        break
                    category = failure
                    meta["retry_used"] = True
                    meta["recovery_cause"] = failure
                    save()
                    protect("failed segment; checkpoint unvalidated")
                    for suffix in (".gjf", ".log", ".chk"):
                        path = dest / (stage + suffix)
                        if path.exists():
                            path.rename(dest / (stage + "-before-recovery" + suffix))
                    if stage == "opt":
                        meta["optimization_restart"] = geometry_restart(
                            source,
                            dest / (stage + "-before-recovery.log"),
                            dest,
                            row,
                            previous_log=old / "opt.log" if old else None,
                            fallback_input=dest / (stage + "-before-recovery.gjf"),
                        )
                    else:
                        shutil.copy2(source, target)
                    recovery = failure
                    save()
            checked = check(log, stage, row)
            meta["executed_input_hashes"][stage] = sha256(target)
            meta["log_hashes"][stage] = sha256(log)
            if checkpoint.exists():
                meta["checkpoint_hashes"][stage] = sha256(checkpoint)
            meta["stage_results"][stage] = {
                k: v for k, v in checked.items() if k != "geometry"
            }
            save()
            if rc or not checked["valid"]:
                category = (
                    failure_kind(log.read_text(errors="replace"), rc)
                    or "scientific_review"
                )
                raise RuntimeError(stage + ": " + "; ".join(checked["issues"]))
            category = "scientific_review"
            if row["kind"] != "ion":
                issues = geometry_issues(checked["geometry"], row)
                if issues:
                    raise RuntimeError("; ".join(issues))
                if stage == "opt":
                    optimized = checked["geometry"]
                elif not same_geometry(optimized, checked["geometry"]):
                    category = "provenance"
                    raise RuntimeError(
                        "Stage geometry differs from verified optimization"
                    )
            category = "file_access"
            checkpoint_readable(
                checkpoint,
                row,
                dest,
                expected_geometry=check(log, stage, row)["geometry"],
            )
            meta.setdefault("validated_checkpoints", {})[stage] = sha256(checkpoint)
            save()
            category = "backup"
            protect()
        meta["status"] = "complete"
        exitcode = 0
        update(args.receipt, args.job, resolved=True)
    except BaseException as exc:
        if isinstance(exc, InterruptedCalculation):
            category = "interrupted"
        meta.update(
            status="interrupted" if category == "interrupted" else "failed",
            error=str(exc),
            terminal_failure=category,
        )
        update(args.receipt, args.job, str(exc), category)
        if category == "file_access":
            write_json(
                dest / "support-evidence.json",
                dict(
                    job=args.job,
                    allocation=meta["slurm_job_id"],
                    environment=meta.get("environment"),
                    input_hashes=meta["executed_input_hashes"],
                    terminal_error=str(exc),
                    stage=meta.get("current_stage"),
                    log_tail=(
                        log.read_text(errors="replace")[-16000:]
                        if "log" in locals() and log.exists()
                        else ""
                    ),
                    support_contacted=False,
                ),
            )
        print(str(exc), flush=True)
    finally:
        try:
            supervisor.stop()
        except Exception as exc:
            backup_safe = False
            meta["shutdown_error"] = str(exc)
            meta["status"] = "failed"
            update(args.receipt, args.job, str(exc), "backup")
        if scratch:
            meta["environment"] = telemetry(scratch)
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        save()
        if backup_safe:
            try:
                protect("terminal " + meta["status"])
            except Exception as exc:
                meta.update(
                    status="failed", terminal_failure="backup", backup_error=str(exc)
                )
                save()
                exitcode = 1
        else:
            exitcode = 1
        supervisor.restore()
        try:
            rebalance(args.receipt)
        except Exception as exc:
            print("Rebalance deferred:", str(exc))
        lock.close()
    raise SystemExit(exitcode)


if __name__ == "__main__":
    main()
