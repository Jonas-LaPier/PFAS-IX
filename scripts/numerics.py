import hashlib
import re
import math
import json
from pathlib import Path

from check_log import NUMBER, last_geometry, number, terminal_failure
from common import ROOT, sha256, write_json
from validate import SYMBOLS, inspect_structure, structure

PROFILE = "internal-20261005"
STEP_LIMIT = 300


def failure_kind(text, returncode=None):
    return terminal_failure(text, returncode)


def algorithm(row):
    return "YQC" if row.get("resin") not in (None, "", "A400") else "XQC"


def controls(
    path,
    stage,
    recovery=None,
    row=None,
    remaining=STEP_LIMIT,
    pcm_solver=False,
    initial_hessian="analytic",
):
    if initial_hessian not in ("analytic", "estimated"):
        raise RuntimeError("Unknown initial Hessian selection")
    text = path.read_text()
    selected = algorithm(row or {})
    if recovery == "scf":
        selected = "XQC" if selected == "YQC" else "YQC"
    scf = f"SCF=({selected},Tight,NoVarAcc,MaxCycle=512,MaxConventional=128)"
    text, count = re.subn(r"SCF=\([^\n)]*\)", scf, text, count=1)
    if count != 1:
        raise RuntimeError("Expected one SCF directive")
    step = 5 if recovery == "pcm" or initial_hessian == "estimated" else 10
    if stage == "opt":
        if not 1 <= remaining <= STEP_LIMIT:
            raise RuntimeError("Optimization step budget exhausted")
        hessian = "CalcFC," if initial_hessian == "analytic" else ""
        text = re.sub(r"\s*IOp\(1/152=\d+\)", "", text, flags=re.I)
        text, count = re.subn(
            r"Opt=\([^\n)]*\)",
            f"Opt=(Redundant,{hessian}"
            f"Tight,MaxStep={step},MaxCycles={remaining}) "
            f"IOp(1/152={remaining})",
            text,
            count=1,
        )
        if count != 1:
            raise RuntimeError("Expected one optimization directive")
    if recovery == "pcm" or pcm_solver:
        text = iterative_pcm(text)
    path.write_text(text)
    return {
        "profile": PROFILE,
        "scf": scf,
        "pcm_solver": "iterative" if "Iterative QConv=VeryTight" in text else "default",
        "optimization_max_steps": remaining if stage == "opt" else None,
        "initial_trust_radius": step / 100 if stage == "opt" else None,
        "initial_hessian": initial_hessian if stage == "opt" else None,
        "input_sha256": sha256(path),
    }


def audit_estimated_hessian(prior, continuation, row, source, evidence_path):
    from backup import verify_snapshot
    from provenance import input_model

    previous = json.loads((prior / "run.json").read_text())
    failed = prior / "opt.log"
    executed = prior / "opt.gjf"
    text = failed.read_text(errors="replace")
    executed_text = executed.read_text()
    segments = previous.get("recovery_segments", [])
    if (
        previous.get("status") != "failed"
        or previous.get("terminal_failure") != "pcm"
        or previous.get("job") != row["job"]
        or previous.get("package_hashes") != continuation["package_hashes"]
        or previous.get("optimization_steps") != 0
        or step_count(text) != 0
        or re.search(r"SCF Done:", text)
        or not segments
        or segments[-1]["log_sha256"] != sha256(failed)
        or segments[-1]["input_sha256"] != sha256(executed)
        or "Force inversion solution in PCM." not in text
        or "Solution method      : Matrix inversion." not in text
        or "Inv3 failed in PCMMkU." not in text
        or "CalcFC" not in executed_text
        or "Iterative QConv=VeryTight" not in executed_text
        or input_model(executed_text, "opt")
        != input_model(source.read_text(), "opt")
    ):
        raise RuntimeError("Estimated Hessian requires the verified zero-step PCM failure")
    backup = json.loads((prior / "backup.json").read_text())
    if backup["status"] != "verified":
        raise RuntimeError("Failed PCM attempt was not preserved")
    names = ["run.json", "opt.log", "opt.gjf"]
    stored = verify_snapshot(backup["manifest"], ["attempt/" + n for n in names])
    if any(stored["files"]["attempt/" + n] != sha256(prior / n) for n in names):
        raise RuntimeError("Preserved PCM failure differs")
    evidence = json.loads(evidence_path.read_text())
    if (
        evidence.get("status") != "passed"
        or not 0 <= evidence["energy_delta_hartree"] < 1e-7
        or not 0 <= evidence["max_force_delta_au"] < 1e-6
    ):
        raise RuntimeError("PCM solver comparison did not pass")
    receipt_path = Path(evidence["receipt"])
    if sha256(receipt_path) != evidence["receipt_sha256"]:
        raise RuntimeError("PCM diagnostic receipt changed")
    case = receipt_path.parent / "PCy3_iterative"
    tested = evidence["cases"]["PCy3_iterative"]
    force_text = (case / "force.log").read_text()
    if (
        sha256(case / "force.gjf") != tested["input_sha256"]
        or sha256(case / "force.log") != tested["log_sha256"]
        or "Solution method      : Iterative solution." not in force_text
        or failure_kind(force_text) is not None
        or executed_text.split("\n\n", 2)[2].split("\n\n", 1)[0]
        != (case / "force.gjf").read_text().split("\n\n", 2)[2].split("\n\n", 1)[0]
    ):
        raise RuntimeError("PCM gradient evidence or starting geometry differs")
    return previous


def evaluated_frames(text):
    frames = []
    pattern = r"^\s*Maximum Force\s+(" + NUMBER + r")\s+(" + NUMBER + r")\s+(YES|NO)"
    previous = 0
    for index, match in enumerate(re.finditer(pattern, text, re.MULTILINE)):
        prefix = text[previous : match.start()]
        previous = match.end()
        atoms = last_geometry(prefix)
        energy = re.findall(r"SCF Done:.*?=\s*(" + NUMBER + r")", prefix)
        rms = re.search(
            r"RMS\s+Force\s+(" + NUMBER + r")", text[match.end() : match.end() + 500]
        )
        if not atoms or not energy or not rms:
            continue
        item = dict(
            step=index + 1,
            geometry=atoms,
            maximum_force=number(match[1]),
            rms_force=number(rms[1]),
            energy_hartree=number(energy[-1]),
            log_offset=match.start(),
        )
        if all(math.isfinite(v) for a in atoms for v in a[1:]) and all(
            math.isfinite(item[k])
            for k in ("maximum_force", "rms_force", "energy_hartree")
        ):
            frames.append(item)
    return frames


def step_count(text):
    return len(re.findall(r"^\s*Maximum Force\s+" + NUMBER, text, re.MULTILINE))


def allocated_steps(text):
    values = re.findall(r"maximum allowed number of steps=\s*(\d+)", text)
    return int(values[0]) if values else None


def optimization_limit(text):
    values = re.findall(r"Step number\s+\d+\s+out of (?:a )?maximum of\s+(\d+)", text)
    return int(values[-1]) if values else None


def best_frame(text, row, root=ROOT):
    data, numbers, _ = structure(root / "structures" / (row["structure"] + ".cjson"))
    from provenance import geometry_issues

    candidates = [
        f
        for f in evaluated_frames(text)
        if [a[0] for a in f["geometry"]] == numbers
        and not geometry_issues(f["geometry"], row, root)
    ]
    if not candidates:
        raise RuntimeError("No structurally valid, fully evaluated restart geometry")
    return min(
        candidates,
        key=lambda f: (
            f["maximum_force"],
            f["rms_force"],
            f["energy_hartree"],
            -f["step"],
        ),
    )


def geometry_restart(
    source, log, dest, row, root=ROOT, previous_log=None, fallback_input=None
):
    from provenance import geometry_issues, input_model

    text = log.read_text(errors="replace")
    candidates = []
    for rank, candidate in enumerate([previous_log, log]):
        if candidate is None or not candidate.exists():
            continue
        try:
            frame = best_frame(candidate.read_text(errors="replace"), row, root)
        except RuntimeError:
            continue
        candidates.append((frame, candidate, rank))
    selected_log = log
    if not candidates:
        if evaluated_frames(text):
            raise RuntimeError(
                "No structurally valid, fully evaluated restart geometry"
            )
        if failure_kind(text) != "scf" and not (
            failure_kind(text) == "pcm" and fallback_input is not None
        ):
            raise RuntimeError("No evaluated geometry available for recovery")
        starting = fallback_input or source
        if input_model(starting.read_text(), "opt") != input_model(
            source.read_text(), "opt"
        ):
            raise RuntimeError("Starting input chemistry differs")
        body = starting.read_text().split("\n\n", 2)[2].split("\n\n", 1)[0]
        numbers = {symbol: number for number, symbol in SYMBOLS.items()}
        atoms = [
            [numbers[line.split()[0]], *map(float, line.split()[1:])]
            for line in body.splitlines()[1:]
        ]
        if geometry_issues(atoms, row, root):
            raise RuntimeError("Starting geometry requires structural review")
        target = dest / "opt.gjf"
        target.write_text(starting.read_text())
        record = {
            "mode": "Verified starting geometry; no new evaluated step",
            "step": 0,
            "starting_input": str(starting),
            "starting_input_sha256": sha256(starting),
        }
    else:
        record, selected_log, _ = min(
            candidates,
            key=lambda item: (
                item[0]["maximum_force"],
                item[0]["rms_force"],
                item[0]["energy_hartree"],
                -item[2],
                -item[0]["step"],
            ),
        )
        prefix, title, body = source.read_text().split("\n\n", 2)
        basis = body.split("\n\n", 1)[1]
        coordinates = "\n".join(
            SYMBOLS[a[0]] + " " + " ".join(f"{v:.10f}" for v in a[1:])
            for a in record["geometry"]
        )
        target = dest / "opt.gjf"
        target.write_text(
            prefix
            + "\n\n"
            + title
            + "\n\n"
            + row["charge"]
            + " 1\n"
            + coordinates
            + "\n\n"
            + basis
        )
        record["mode"] = "Best evaluated geometry; fresh Hessian and wavefunction"
    record.update(
        source_log=str(selected_log),
        source_log_sha256=sha256(selected_log),
        failed_log=str(log),
        failed_log_sha256=sha256(log),
        input_sha256=sha256(target),
    )
    write_json(dest / "recovery-geometry.json", record)
    return record


def iterative_pcm(text):
    normalized = normalize_pcm(text)
    if "SCRF=(SMD,Solvent=Water)" not in normalized:
        raise ValueError("Iterative PCM requires the existing SMD water model")
    return (
        normalized.replace(
            "SCRF=(SMD,Solvent=Water)", "SCRF=(SMD,Solvent=Water,Read)"
        ).rstrip()
        + "\n\nIterative QConv=VeryTight\n\n"
    )


def normalize_pcm(text):
    prefix, rest = text.split("\n\n", 1)
    routes = re.findall(r"SCRF=\([^)]*\)", prefix, re.I)
    if not routes:
        return text
    if len(routes) != 1:
        raise ValueError("Expected one solvent directive")
    route = routes[0]
    if "READ" not in route.upper().removeprefix("SCRF=(").removesuffix(")").split(","):
        return text
    if route.upper() != "SCRF=(SMD,SOLVENT=WATER,READ)":
        raise ValueError("Unapproved solvent model or options")
    sections = rest.rstrip().rsplit("\n\n", 1)
    if len(sections) != 2 or sections[1] != "Iterative QConv=VeryTight":
        raise ValueError("Unapproved PCM parameters")
    return (
        prefix.replace(route, "SCRF=(SMD,Solvent=Water)")
        + "\n\n"
        + sections[0]
        + "\n\n"
    )


def scientific_input(text):
    text = normalize_pcm(text)
    prefix, rest = text.split("\n\n", 1)
    body = rest if "GEOM=ALLCHECK" in prefix.upper() else rest.split("\n\n", 1)[1]
    for directive in re.findall(r"^%[^\n]+", prefix, flags=re.MULTILINE):
        if not re.fullmatch(
            r"%(NProcShared=\d+|Mem=\d+GB|(?:Old)?Chk=[a-z]+\.chk)", directive, re.I
        ):
            raise ValueError("Unapproved Link0 directive")
    for token in re.findall(r"(?:SCF|Opt)=\([^)]*\)|IOp\([^)]*\)", prefix, flags=re.I):
        if token.upper().startswith("SCF"):
            allowed = r"SCF=\((?:XQC|YQC),Tight(?:,NoVarAcc,MaxCycle=512,MaxConventional=128)?\)"
        elif token.upper().startswith("OPT"):
            allowed = r"Opt=\((?:(?:Cartesian|Redundant),CalcFC,Tight(?:,MaxStep=(?:5|10))?|Redundant,Tight,MaxStep=5),MaxCycles=(?:[1-9]\d?|[12]\d\d|300)\)"
        else:
            allowed = r"IOp\(1/152=(?:[1-9]\d?|[12]\d\d|300)\)"
        if not re.fullmatch(allowed, token, re.I):
            raise ValueError("Unapproved numerical directive: " + token)
    prefix = re.sub(
        r"^%(?:NProcShared|Mem)=[^\n]*\n?", "", prefix, flags=re.MULTILINE | re.I
    )
    prefix = re.sub(
        r"SCF=\([^)]*\)|Opt=\([^)]*\)|IOp\(1/152=\d+\)", "", prefix, flags=re.I
    )
    return " ".join(prefix.split()).upper() + "\n" + " ".join(body.split())


def compatible_inputs(old, new):
    return scientific_input(old.read_text()) == scientific_input(new.read_text())


def scientific_hash(path):
    return hashlib.sha256(scientific_input(path.read_text()).encode()).hexdigest()
