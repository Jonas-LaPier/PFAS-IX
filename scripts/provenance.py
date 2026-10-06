import json
import re
import subprocess
from pathlib import Path

from analyze import same_geometry
from backup import verify_snapshot
from check_log import check
from common import ROOT, input_hashes, runtime_files, sha256, stages
from numerics import scientific_input
from validate import RADII, inspect_structure, structure
import math


def geometry_issues(atoms, row, root=ROOT):
    data, numbers, _ = structure(root / "structures" / (row["structure"] + ".cjson"))
    if [a[0] for a in atoms] != numbers:
        return ["Atom identity or order differs"]
    xyz = [a[1:] for a in atoms]
    issues = inspect_structure(data, numbers, xyz)
    bonds = data["bonds"]["connections"]["index"]
    bonded = {tuple(sorted(pair)) for pair in zip(bonds[::2], bonds[1::2])}
    for a in range(len(numbers)):
        for b in range(a):
            if (b, a) not in bonded and math.dist(xyz[a], xyz[b]) < 1.12 * (
                RADII[numbers[a]] + RADII[numbers[b]]
            ):
                issues.append(f"Possible new covalent bond {b}-{a}")
    centers = [
        i
        for i, (n, q) in enumerate(zip(numbers, data["atoms"]["formalCharges"]))
        if n in (7, 15) and q == 1
    ]
    for i, n in enumerate(numbers):
        if centers and (n in (17, 53) or (n == 9 and row["kind"] == "counterion")):
            if min(math.dist(xyz[i], xyz[c]) for c in centers) > 6:
                issues.append("Dissociated counterion; scientific review required")
    oxygens = [i for i, n in enumerate(numbers) if n == 8]
    if (
        row["kind"] == "complex"
        and min(math.dist(xyz[c], xyz[o]) for c in centers for o in oxygens) > 5.5
    ):
        issues.append("Dissociated PFAS; scientific review required")
    return issues


def input_model(text, stage):
    if stage == "opt":
        prefix, title, body = text.split("\n\n", 2)
        coordinates, basis = body.split("\n\n", 1)
        lines = coordinates.splitlines()
        identities = "\n".join(line.split()[0] for line in lines[1:])
        text = (
            prefix
            + "\n\n"
            + title
            + "\n\n"
            + lines[0]
            + "\n"
            + identities
            + "\n\n"
            + basis
        )
    return scientific_input(text)


def source_audit(descriptor, row, root=ROOT):
    folder = Path(descriptor["attempt"])
    package = Path(descriptor["package"])
    receipt = Path(descriptor["receipt"])
    for key, path in [
        ("meta_sha256", folder / "run.json"),
        ("receipt_sha256", receipt),
    ]:
        if sha256(path) != descriptor[key]:
            raise RuntimeError("Recovery provenance changed: " + str(path))
    old = json.loads((folder / "run.json").read_text())
    submission = json.loads(receipt.read_text())
    if (
        sha256(package / "checks/SHA256SUMS.json")
        != descriptor["package_checksums_sha256"]
    ):
        raise RuntimeError("Original package checksum record changed")
    frozen = json.loads((package / "checks/SHA256SUMS.json").read_text())
    if set(frozen) != {str(p.relative_to(package)) for p in runtime_files(package)}:
        raise RuntimeError("Original package inventory differs")
    for name, digest in frozen.items():
        if sha256(package / name) != digest:
            raise RuntimeError("Original frozen package changed: " + name)
    if sha256(package / "structures" / (row["structure"] + ".cjson")) != sha256(
        root / "structures" / (row["structure"] + ".cjson")
    ):
        raise RuntimeError("Recovery structure changed")
    if descriptor.get("diagnostic"):
        case = next(c for c in submission["cases"] if c["name"] == folder.name)
        if case["row"] != row:
            comparable = dict(row)
            comparable["hours"] = case["row"]["hours"]
            if case["row"] != comparable:
                raise RuntimeError("Diagnostic workflow differs")
        if old.get("input_hashes") != case["input_hashes"]:
            raise RuntimeError("Diagnostic input provenance mismatch")
    else:
        if old.get("job") != row["job"] or old.get("input_hashes") != input_hashes(
            row, package
        ):
            raise RuntimeError("Original attempt provenance mismatch")
        if old.get("package_hashes") != submission["package_hashes"]:
            raise RuntimeError("Original package provenance mismatch")
    backup = Path(descriptor["backup_manifest"])
    if sha256(backup) != descriptor["backup_manifest_sha256"]:
        raise RuntimeError("Preservation manifest changed")
    mapping = json.loads(backup.read_text())["files"]
    for name in ("run.json", "opt.log", "opt.gjf"):
        path = folder / name
        if path.is_file():
            if mapping.get("attempt/" + name) != sha256(path):
                raise RuntimeError("Preserved source changed: " + name)
    reusable = []
    optimized = None
    for stage in stages(row):
        baseline = package / row["input_dir"] / (stage + ".gjf")
        current = root / row["input_dir"] / (stage + ".gjf")
        if scientific_input(baseline.read_text()) != scientific_input(
            current.read_text()
        ):
            raise RuntimeError("Scientific inputs changed: " + stage)
        log, chk, executed = [
            folder / (stage + suffix) for suffix in (".log", ".chk", ".gjf")
        ]
        if not all(p.is_file() for p in (log, chk, executed)):
            break
        result = check(log, stage, row)
        if not result["valid"]:
            break
        for path in (log, chk, executed):
            if mapping.get("attempt/" + path.name) != sha256(path):
                raise RuntimeError("Preserved stage changed: " + str(path))
        if old.get("log_hashes", {}).get(stage) != sha256(log) or old.get(
            "checkpoint_hashes", {}
        ).get(stage) != sha256(chk):
            raise RuntimeError("Completed stage hashes do not match")
        expected = old.get("executed_input_hashes", {}).get(
            stage, old.get("input_hashes", {}).get(stage)
        )
        if expected != sha256(executed) and not (
            old.get("numerical_controls", {}).get(stage, {}).get("input_sha256")
            == sha256(executed)
        ):
            raise RuntimeError("Executed input was not recorded")
        if input_model(executed.read_text(), stage) != input_model(
            current.read_text(), stage
        ):
            raise RuntimeError("Executed chemistry differs")
        if stage == "opt":
            optimized = result["geometry"]
        elif row["kind"] != "ion" and not same_geometry(optimized, result["geometry"]):
            raise RuntimeError("Reused stage geometry mismatch")
        if row["kind"] != "ion" and geometry_issues(result["geometry"], row, root):
            raise RuntimeError("Recovered result requires structural review")
        verify_snapshot(backup, ["attempt/" + p.name for p in (log, chk, executed)])
        reusable.append(stage)
    return reusable


def checkpoint_readable(checkpoint, row, folder, root=ROOT, expected_geometry=None):
    output = folder / (checkpoint.stem + "-verified.fchk")
    with (folder / (checkpoint.stem + "-formchk.log")).open("w") as log:
        result = subprocess.run(
            ["formchk", str(checkpoint), str(output)],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=300,
        )
    if result.returncode or not output.exists():
        raise RuntimeError("Checkpoint is unreadable")
    text = output.read_text()
    for label, value in [
        ("Number of atoms", int(row["atoms"])),
        ("Charge", int(row["charge"])),
        ("Multiplicity", 1),
    ]:
        m = re.search(r"^" + label + r"\s+I\s+(-?\d+)", text, re.M)
        if not m or int(m[1]) != value:
            raise RuntimeError("Checkpoint " + label + " differs")
    m = re.search(
        r"^Atomic numbers\s+I\s+N=\s*\d+\s*\n(.*?)(?=^[A-Za-z]|\Z)", text, re.M | re.S
    )
    numbers = [int(n) for n in m[1].split()] if m else []
    expected = structure(root / "structures" / (row["structure"] + ".cjson"))[1]
    if numbers != expected:
        raise RuntimeError("Checkpoint atom order differs")
    if expected_geometry:
        m = re.search(
            r"^Current cartesian coordinates\s+R\s+N=\s*\d+\s*\n(.*?)(?=^[A-Za-z]|\Z)",
            text,
            re.M | re.S,
        )
        values = (
            [float(v.replace("D", "E")) * 0.529177210903 for v in m[1].split()]
            if m
            else []
        )
        atoms = [[n, *values[i * 3 : i * 3 + 3]] for i, n in enumerate(numbers)]
        if len(values) != 3 * len(numbers) or not same_geometry(
            atoms, expected_geometry
        ):
            raise RuntimeError("Checkpoint geometry differs from validated log")
    return sha256(checkpoint)
