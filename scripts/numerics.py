import json
import re
import shutil

from check_log import last_geometry
from common import ROOT, sha256
from validate import SYMBOLS, inspect_structure, structure


def failure_kind(text):
    if "Inv3 failed in PCMMkU" in text:
        return "pcm"
    if "No lower point found" in text or "Convergence failure" in text:
        return "scf"
    if "Number of steps exceeded" in text:
        return "steps"
    return None


def controls(path, stage, recovery=None):
    text = path.read_text()
    algorithm = "YQC" if recovery == "scf" else "XQC"
    scf = f"SCF=({algorithm},Tight,NoVarAcc,MaxCycle=512,MaxConventional=128)"
    text, count = re.subn(r"SCF=\([^\n)]*\)", scf, text, count=1)
    if count != 1:
        raise RuntimeError("Expected one SCF directive")
    if stage == "opt":
        text, count = re.subn(
            r"Opt=\([^\n)]*\)",
            "Opt=(Cartesian,CalcFC,Tight,MaxCycles=300"
            + (",MaxStep=10" if recovery == "pcm" else "")
            + ") IOp(1/152=300)",
            text,
            count=1,
        )
        if count != 1:
            raise RuntimeError("Expected one optimization directive")
    path.write_text(text)
    return {
        "scf": scf,
        "optimization_max_steps": 300 if stage == "opt" else None,
        "max_step_bohr": 0.1 if recovery == "pcm" and stage == "opt" else None,
        "input_sha256": sha256(path),
    }


def geometry_restart(source, log, dest, row):
    text = log.read_text(errors="replace")
    forces = list(re.finditer(r"^\s*Maximum Force\s+", text, re.MULTILINE))
    if not forces:
        if failure_kind(text) == "scf":
            target = dest / "opt.gjf"
            shutil.copy2(source, target)
            return {
                "mode": "Fresh wavefunction from validated initial geometry",
                "source_log": str(log),
                "source_log_sha256": sha256(log),
                "input_sha256": sha256(target),
            }
        raise RuntimeError("No accepted geometry with evaluated forces to restart")
    atoms = last_geometry(text[: forces[-1].start()])
    data, numbers, _ = structure(ROOT / "structures" / (row["structure"] + ".cjson"))
    if [a[0] for a in atoms] != numbers:
        raise RuntimeError("Recovery geometry atom order differs")
    issues = inspect_structure(data, numbers, [a[1:] for a in atoms])
    if issues:
        raise RuntimeError(
            "Recovery geometry requires inspection: " + "; ".join(issues)
        )
    prefix, body, basis = source.read_text().split("\n\n", 2)
    basis = basis.split("\n\n", 1)[1]
    coordinates = "\n".join(
        SYMBOLS[a[0]] + " " + " ".join(f"{v:.10f}" for v in a[1:]) for a in atoms
    )
    target = dest / "opt.gjf"
    target.write_text(
        prefix
        + "\n\n"
        + body
        + "\n\n"
        + row["charge"]
        + " 1\n"
        + coordinates
        + "\n\n"
        + basis
    )
    record = {
        "mode": "Fresh optimization and wavefunction from last evaluated geometry",
        "source_log": str(log),
        "source_log_sha256": sha256(log),
        "geometry": atoms,
        "input_sha256": sha256(target),
    }
    (dest / "recovery-geometry.json").write_text(json.dumps(record, indent=2) + "\n")
    return record
