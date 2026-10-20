import argparse
import fcntl
import json
import math
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from backup import snapshot, storage_status, verify_snapshot
from check_log import last_geometry, number, terminal_failure
from common import ROOT, manifest, runtime_files, sha256, write_json
from numerics import cartesian_forces, evaluated_frames, step_count
from provenance import checkpoint_readable, geometry_issues, input_model
from supervisor import InterruptedCalculation, Supervisor, telemetry
from validate import SYMBOLS


class PrerequisiteFailed(RuntimeError):
    pass


class ComparisonFailed(RuntimeError):
    def __init__(self, result):
        self.result = result
        super().__init__("Matched energy/force repeat requires review: " +
                         str(result["reference_energy_delta"]) + ", " +
                         str(result["reference_max_force_delta"]))


def validate_repeat_reference(case, inp):
    evidence = case.get("comparison_source")
    if not evidence:
        raise RuntimeError("A repeat comparison requires verified identical-input evidence")
    prior = Path(evidence["attempt"])
    record = Path(evidence["backup_manifest"])
    if sha256(record) != evidence["backup_manifest_sha256"]:
        raise RuntimeError("Comparison backup manifest changed")
    files = ("calc.gjf", "calc.log", "run.json")
    saved = verify_snapshot(record, ["attempt/" + name for name in files])
    for name in files:
        digest = sha256(prior / name)
        if digest != evidence["files"][name] or saved["files"]["attempt/" + name] != digest:
            raise RuntimeError("Comparison evidence differs from its backup")
    previous = json.loads((prior / "run.json").read_text())
    if (previous.get("kind") != "force" or previous.get("job") != case["job"]
            or previous.get("input_sha256") != sha256(prior / "calc.gjf")
            or previous.get("log_sha256") != sha256(prior / "calc.log")):
        raise RuntimeError("Comparison run provenance differs")
    if previous["status"] != "passed" and not (
        previous["status"] == "failed"
        and previous.get("error", "").startswith("Energy/force comparison requires review:")
        and previous.get("terminal_failure") is None
        and not previous.get("backup_error") and not previous.get("shutdown_error")
    ):
        raise RuntimeError("Comparison source has an unresolved calculation failure")
    text = (prior / "calc.log").read_text()
    if terminal_failure(text) is not None or inp.read_bytes() != (prior / "calc.gjf").read_bytes():
        raise RuntimeError("Repeat comparison requires normal termination and identical executed inputs")
    result = force_result(text, {"geometry": case["geometry"]})
    expected = {key: result[key] for key in ("energy_hartree", "cartesian_forces")}
    if case["reference"] != expected:
        raise RuntimeError("Comparison values differ from the verified log")
    version = re.search(r"Gaussian 16:\s+([^\n]+)", text)
    if not version or case.get("reference_version") != version[1].strip():
        raise RuntimeError("Comparison Gaussian version differs")


def make_input(source, atoms, kind, precise=True, reuse_guess=False, reuse_hessian=False):
    if kind not in ("force", "opt", "hessian"):
        raise ValueError("Unknown bounded calculation")
    if reuse_hessian and (kind != "opt" or not reuse_guess):
        raise ValueError("Hessian reuse requires an optimization and validated guess")
    prefix, title, body = source.split("\n\n", 2)
    coordinates, basis = body.split("\n\n", 1)
    charge = coordinates.splitlines()[0]
    scf = "SCF=(XQC,Tight,NoVarAcc,MaxCycle=512,MaxConventional=32"
    scf += ",NoIncFock)" if precise else ")"
    prefix, count = re.subn(r"SCF=\([^)]*\)", scf, prefix)
    if count != 1:
        raise ValueError("Expected one SCF directive")
    if precise:
        prefix, count = re.subn(r"Integral=UltraFine\b", "Integral=(UltraFine,Acc2E=14)", prefix)
        if count != 1:
            raise ValueError("Expected UltraFine grid")
    prefix = re.sub(r"\s*IOp\(1/152=\d+\)", "", prefix)
    keyword = {
        "force": "Force",
        "hessian": "Freq=NoRaman",
        "opt": "Opt=(Redundant,Tight,MaxStep=5,NoTrustUpdate,MaxCycles=8) IOp(1/152=8)",
    }[kind]
    prefix, count = re.subn(r"Opt=\([^)]*\)", keyword, prefix)
    if count != 1 or "Guess=" in prefix or "Geom=" in prefix:
        raise ValueError("Expected explicit geometry and fresh guess")
    if reuse_hessian:
        prefix = prefix.replace("Opt=(Redundant,", "Opt=(Redundant,ReadFC,")
    if reuse_guess:
        prefix += " Guess=Read"
    prefix = re.sub(r"%Chk=.*", "%Chk=calc.chk", prefix)
    xyz = "\n".join(SYMBOLS[a[0]] + " " + " ".join(f"{v:.10f}" for v in a[1:]) for a in atoms)
    return prefix + "\n\n" + title + "\n\n" + charge + "\n" + xyz + "\n\n" + basis


def validate_case(case, folder, receipt):
    limit = (12 if case["kind"] == "force" else 24) * 3600 - 1800
    if not 0 < case["wall_seconds"] <= limit:
        raise RuntimeError("Diagnostic wall-time budget exceeds its hard limit")
    row = next(r for r in manifest() if r["job"] == case["job"])
    if geometry_issues(case["geometry"], row):
        raise RuntimeError("Diagnostic geometry fails structural checks")
    source = folder / "sources" / case["source_input"]
    if sha256(source) != case["source_input_sha256"]:
        raise RuntimeError("Diagnostic source input changed")
    if input_model(source.read_text(), "opt") != input_model((ROOT / row["input_dir"] / "opt.gjf").read_text(), "opt"):
        raise RuntimeError("Diagnostic source scientific model changed")
    expected = make_input(source.read_text(), case["geometry"], case["kind"], case["precise"],
                          bool(case.get("reuse_from")), case.get("reuse_hessian", False))
    inp = folder / case["name"] / "calc.gjf"
    if inp.read_text() != expected or sha256(inp) != case["input_sha256"]:
        raise RuntimeError("Executed input differs from bounded plan")
    if case.get("reference"):
        if case["kind"] != "force":
            raise RuntimeError("Repeat comparison is only supported for fixed-geometry forces")
        validate_repeat_reference(case, inp)
    old = Path(case["source_attempt"])
    backup = Path(case["source_backup_manifest"])
    if sha256(backup) != case["source_backup_sha256"]:
        raise RuntimeError("Source backup changed")
    mapping = json.loads(backup.read_text())["files"]
    for name, digest in case["source_files"].items():
        if sha256(old / name) != digest or mapping.get("attempt/" + name) != digest:
            raise RuntimeError("Source evidence differs from verified backup")
    for name, digest in receipt["package_hashes"].items():
        if sha256(ROOT / name) != digest:
            raise RuntimeError("Frozen runtime package changed")
    if set(receipt["package_hashes"]) != {str(p.relative_to(ROOT)) for p in runtime_files()}:
        raise RuntimeError("Frozen package inventory changed")
    return row, inp


def prior_gate(folder, case):
    if list(folder.glob("halt-*.json")):
        raise PrerequisiteFailed("Diagnostic infrastructure stop is active")
    for name in case.get("requires", []):
        prior = folder / name
        meta = json.loads((prior / "run.json").read_text())
        saved = json.loads((prior / "backup.json").read_text())
        if meta["status"] != "passed" or saved["status"] != "verified":
            raise PrerequisiteFailed("Prerequisite did not pass: " + name)
        path = Path(saved["manifest"])
        if sha256(path) != saved["manifest_sha256"]:
            raise RuntimeError("Prerequisite backup changed")
        mapping = verify_snapshot(path, ["attempt/run.json", "attempt/calc.log", "attempt/calc.gjf"])
        for f in ("run.json", "calc.log", "calc.gjf"):
            if mapping["files"]["attempt/" + f] != sha256(prior / f):
                raise RuntimeError("Prerequisite evidence changed")


def force_result(text, case):
    geometry = last_geometry(text)
    forces = cartesian_forces(text, geometry)
    energies = re.findall(r"SCF Done:[^\n]*=\s*([-+\d.DEde]+)", text)
    if not energies or len(geometry) != len(case["geometry"]):
        raise RuntimeError("Incomplete energy or geometry")
    if [a[0] for a in geometry] != [a[0] for a in case["geometry"]]:
        raise RuntimeError("Fixed geometry atom identities differ")
    if max(abs(a-b) for x,y in zip(geometry,case["geometry"]) for a,b in zip(x[1:],y[1:])) > 2e-6:
        raise RuntimeError("Fixed geometry moved")
    result = dict(energy_hartree=number(energies[-1]), **forces)
    if not math.isfinite(result["energy_hartree"]):
        raise RuntimeError("Nonfinite energy")
    historical = case.get("historical_reference")
    if historical:
        result["historical_energy_delta"] = abs(result["energy_hartree"] - historical["energy_hartree"])
        result["historical_comparison"] = "Different numerical settings or reconstructed coordinates; not an exact-repeat test"
    reference = case.get("reference")
    if reference:
        if case.get("reference_version"):
            version = re.search(r"Gaussian 16:\s+([^\n]+)", text)
            if not version or version[1].strip() != case["reference_version"]:
                raise RuntimeError("Repeat Gaussian version differs")
        if len(reference["cartesian_forces"]) != len(geometry):
            raise RuntimeError("Reference force size differs")
        if any(len(row) != 3 for row in reference["cartesian_forces"]):
            raise RuntimeError("Incomplete reference force vector")
        if not all(math.isfinite(v) for row in reference["cartesian_forces"] for v in row) or not math.isfinite(reference["energy_hartree"]):
            raise RuntimeError("Nonfinite comparison reference")
        delta = abs(result["energy_hartree"] - reference["energy_hartree"])
        df = max(abs(a-b) for x,y in zip(forces["cartesian_forces"],reference["cartesian_forces"]) for a,b in zip(x,y))
        result.update(reference_energy_delta=delta, reference_max_force_delta=df)
        if delta > 1e-7 or df > 1e-6:
            raise ComparisonFailed(result)
        result["repeat_comparison"] = "passed"
    return result


def run(folder, name):
    receipt_path = folder / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    case = next(c for c in receipt["cases"] if c["name"] == name)
    if not os.environ.get("SLURM_JOB_ID") or int(os.environ.get("SLURM_CPUS_PER_TASK", "0")) != 8:
        raise RuntimeError("Bounded tests require an eight-CPU Slurm allocation")
    if not json.loads(Path(receipt["production_health"]).read_text()).get("stopped"):
        raise RuntimeError("Full campaign must remain held")
    dest = folder / name
    lock = (dest / "case.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if (dest / "run.json").exists():
        raise RuntimeError("Diagnostic already attempted; automatic retry forbidden")
    meta = dict(case=name, job=case["job"], kind=case["kind"], status="running",
                slurm_job_id=os.environ["SLURM_JOB_ID"], started=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                receipt_sha256=sha256(receipt_path), scientific_workflow_complete=False)
    supervisor = Supervisor(interval=4*3600)
    supervisor.next_snapshot = time.monotonic() + 300
    supervisor.install()
    scratch = None
    safe = True
    code = 1

    def save():
        write_json(dest / "run.json", meta)

    def protect(reason):
        meta["snapshot_reason"] = reason
        save()
        try:
            saved = Path(snapshot(dest, receipt_path))
            verify_snapshot(saved)
        except Exception as exc:
            write_json(folder / ("halt-" + name + ".json"), dict(case=name, category="backup", error=str(exc)))
            raise
        write_json(dest / "backup.json", dict(status="verified", manifest=str(saved), manifest_sha256=sha256(saved), reason=reason))
        print(json.dumps(dict(case=name, event="backup_verified", reason=reason, manifest=str(saved))), flush=True)

    try:
        save()
        row, inp = validate_case(case, folder, receipt)
        prior_gate(folder, case)
        if case.get("reuse_from"):
            prior = folder / case["reuse_from"]
            previous = json.loads((prior / "run.json").read_text())
            if case["reuse_from"] not in case.get("requires", []):
                raise RuntimeError("Checkpoint source is not a verified prerequisite")
            if case.get("reuse_hessian") and previous["kind"] != "hessian":
                raise RuntimeError("ReadFC requires the completed Hessian probe")
            checkpoint = prior / "calc.chk"
            if sha256(checkpoint) != previous["checkpoint_sha256"]:
                raise RuntimeError("Prerequisite checkpoint changed")
            saved = json.loads((prior / "backup.json").read_text())
            verified = verify_snapshot(saved["manifest"], ["attempt/calc.chk"])
            if verified["files"]["attempt/calc.chk"] != sha256(checkpoint):
                raise RuntimeError("Prerequisite checkpoint backup differs")
            checkpoint_readable(checkpoint, row, dest, expected_geometry=case["geometry"])
            shutil.copy2(checkpoint, dest / "calc.chk")
        storage_status(new_start=True)
        base = Path(os.environ["SCRATCH"]) / "PFAS-IX-gaussian"
        base.mkdir(exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix="bounded-" + name + "-", dir=base))
        meta.update(environment=telemetry(scratch), input_sha256=sha256(inp))
        save()
        print(json.dumps(dict(case=name, event="gaussian_started", input_sha256=sha256(inp))), flush=True)
        deadline = time.monotonic() + case["wall_seconds"]
        log = dest / "calc.log"

        def observe():
            if time.monotonic() >= deadline:
                raise InterruptedCalculation("Bounded wall-time limit reached")
            if case["kind"] == "opt" and log.exists() and step_count(log.read_text(errors="replace")) >= 8:
                raise InterruptedCalculation("Eight evaluated geometry steps reached")

        with inp.open() as src, log.open("w") as out:
            rc = supervisor.run(["g16"], src, out, dest, dict(os.environ, GAUSS_SCRDIR=str(scratch)), protect, observe)
        text = log.read_text(errors="replace")
        meta["terminal_failure"] = terminal_failure(text, rc)
        if case["kind"] == "opt" and meta["terminal_failure"] == "steps" and step_count(text) >= 8:
            raise InterruptedCalculation("Eight-step Gaussian allocation exhausted")
        if meta["terminal_failure"]:
            raise RuntimeError("Gaussian terminal outcome: " + meta["terminal_failure"])
        if case["kind"] == "force":
            meta["result"] = force_result(text, case)
        elif case["kind"] == "hessian":
            frequencies = [number(v) for line in text.splitlines() if "Frequencies --" in line for v in line.split("--",1)[1].split()]
            if len(frequencies) != 3*int(row["atoms"])-6:
                raise RuntimeError("Incomplete Hessian probe")
            meta["result"] = dict(frequencies=frequencies, minimum_not_established=True)
        else:
            meta["frames"] = evaluated_frames(text)
        if geometry_issues(last_geometry(text), row):
            raise RuntimeError("Result geometry requires scientific review")
        meta["checkpoint_sha256"] = checkpoint_readable(dest / "calc.chk", row, dest, expected_geometry=last_geometry(text))
        meta["status"] = "passed"
        code = 0
    except InterruptedCalculation as exc:
        meta.update(status="bounded_stop", error=str(exc), checkpoint_unvalidated=True)
    except PrerequisiteFailed as exc:
        meta.update(status="blocked", error=str(exc))
    except ComparisonFailed as exc:
        meta.update(status="review_required", error=str(exc), result=exc.result)
    except BaseException as exc:
        meta.update(status="failed", error=str(exc))
        if meta.get("terminal_failure") == "file_access":
            write_json(folder / ("halt-" + name + ".json"), dict(case=name, category="file_access", error=str(exc)))
    finally:
        try:
            supervisor.stop()
        except Exception as exc:
            safe = False
            meta.update(status="failed", shutdown_error=str(exc))
        meta["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if scratch:
            meta["environment"] = telemetry(scratch)
        log = dest / "calc.log"
        if log.exists():
            meta["log_sha256"] = sha256(log)
            if case["kind"] == "opt":
                meta["frames"] = evaluated_frames(log.read_text(errors="replace"))
        save()
        if safe:
            try:
                protect("terminal " + meta["status"])
            except Exception as exc:
                meta.update(status="failed", backup_error=str(exc))
                save()
                write_json(dest / "backup.json", dict(status="failed", error=str(exc)))
                code = 1
        else:
            code = 1
        supervisor.restore()
        print(json.dumps(dict(case=name, event="finished", status=meta["status"], error=meta.get("error"))), flush=True)
    return code


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("name")
    args = parser.parse_args()
    raise SystemExit(run(args.folder.resolve(), args.name))
