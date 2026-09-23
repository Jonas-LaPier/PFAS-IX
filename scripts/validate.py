#!/usr/bin/env python3


import json
import math
from collections import Counter

from common import ROOT, manifest, sha256, stages

PFAS = {
    "PFBA": {6: 4, 9: 7, 8: 2},
    "PFPeA": {6: 5, 9: 9, 8: 2},
    "PFHpA": {6: 7, 9: 13, 8: 2},
    "PFOA": {6: 8, 9: 15, 8: 2},
    "PFNA": {6: 9, 9: 17, 8: 2},
    "PFOS": {6: 8, 9: 17, 8: 3, 16: 1},
}
RADII = {1: 0.31, 6: 0.76, 7: 0.71, 8: 0.66, 9: 0.57, 15: 1.07, 16: 1.05, 17: 1.02}
SYMBOLS = {1: "H", 6: "C", 7: "N", 8: "O", 9: "F", 15: "P", 16: "S", 17: "Cl"}


def structure(path):
    d = json.loads(path.read_text())
    z = d["atoms"]["elements"]["number"]
    flat = d["atoms"]["coords"]["3d"]
    xyz = [flat[i : i + 3] for i in range(0, len(flat), 3)]
    return d, z, xyz


def inspect_structure(d, z, xyz):
    problems = []
    if len(xyz) != len(z) or any(
        len(v) != 3 or not all(math.isfinite(x) for x in v) for v in xyz
    ):
        return ["Invalid coordinates"]
    bonds = d["bonds"]["connections"]["index"]
    adj = [set() for _ in z]
    for a, b in zip(bonds[::2], bonds[1::2]):
        adj[a].add(b)
        adj[b].add(a)
        ratio = math.dist(xyz[a], xyz[b]) / (RADII[z[a]] + RADII[z[b]])
        if not 0.65 < ratio < 1.4:
            problems.append(
                f"Suspicious bond length {a}-{b}: {ratio:.2f} times covalent radii"
            )
    seen = set()
    fragments = []
    for i in range(len(z)):
        if i in seen:
            continue
        todo = [i]
        part = set()
        while todo:
            j = todo.pop()
            if j in part:
                continue
            part.add(j)
            todo.extend(adj[j] - part)
        seen |= part
        fragments.append(part)
    for k, first in enumerate(fragments):
        for second in fragments[k + 1 :]:
            dist = min(math.dist(xyz[i], xyz[j]) for i in first for j in second)
            if dist < 1.45:
                problems.append(f"Interfragment clash: {dist:.3f} A")
    return problems


def validate(root=ROOT):
    errors = []
    rows = manifest(root)
    if len(rows) != 208:
        errors.append("Expected 208 jobs")
    if len({r["job"] for r in rows}) != len(rows):
        errors.append("Duplicate IDs")
    expected = {"complex": 180, "free": 12, "counterion": 12, "ion": 4}
    if dict(Counter(r["kind"] for r in rows)) != expected:
        errors.append("Wrong calculation inventory")
    if Counter(r["batch"] for r in rows) != Counter({"1": 56, "2": 152}):
        errors.append("Wrong batch sizes")
    matrices = Counter(
        (r["resin"], r["pfas"], r["environment"])
        for r in rows
        if r["kind"] == "complex"
    )
    if len(matrices) != 60 or any(v != 3 for v in matrices.values()):
        errors.append("Missing resin/PFAS/solvent poses")
    reference = {}
    for resin in ["A400", "PC4P", "PC4P-Me", "PCy3", "PPh3"]:
        _, z, _ = structure(root / "structures" / (resin + "_Cl.cjson"))
        reference[resin] = Counter(z)
    for r in rows:
        label = r["job"]
        d, z, xyz = structure(root / "structures" / (r["structure"] + ".cjson"))
        errors.extend(label + ": " + x for x in inspect_structure(d, z, xyz))
        if len(z) != int(r["atoms"]):
            errors.append(label + ": manifest atom count")
        charge = sum(d["atoms"]["formalCharges"])
        if charge != int(r["charge"]) or (sum(z) - charge) % 2:
            errors.append(label + ": charge/electron parity")
        if charge != (-1 if r["kind"] in ["free", "ion"] else 0):
            errors.append(label + ": unexpected net charge")
        composition = Counter(z)
        if r["kind"] == "free" and composition != Counter(PFAS[r["pfas"]]):
            errors.append(label + ": PFAS formula mismatch")
        if r["kind"] == "complex":
            expected_composition = reference[r["resin"]].copy()
            expected_composition[17] -= 1
            expected_composition += Counter(PFAS[r["pfas"]])
            if composition != +expected_composition:
                errors.append(label + ": exchange stoichiometry")
            if composition[17] != (1 if r["resin"] == "PC4P-Me" else 0):
                errors.append(label + ": spectator chloride")
        for s in stages(r):
            f = root / r["input_dir"] / (s + ".gjf")
            if not f.exists():
                errors.append(label + ": missing " + s)
                continue
            text = f.read_text()
            water = "SCRF=(SMD,Solvent=Water)" in text
            if water != (r["environment"] == "water"):
                errors.append(label + ": solvent mismatch")
            if (
                f"%NProcShared={r['cpus']}" not in text
                or f"%Mem={r['gaussian_mem_gb']}GB" not in text
            ):
                errors.append(label + ": resource mismatch")
            if int(r["gaussian_mem_gb"]) >= int(r["mem_gb"]):
                errors.append(label + ": no memory overhead")
            method = "PBE1PBE/6-311+G(d,p)" if s == "sp" else "PBEPBE/6-31+G(d,p)"
            if method not in text or "EmpiricalDispersion=GD3BJ" not in text:
                errors.append(label + ": method mismatch")
            if s == "opt" or r["kind"] == "ion":
                lines = text.splitlines()
                marker = f"{r['charge']} 1"
                if marker not in lines:
                    errors.append(label + ": missing molecular charge")
                    continue
                atoms = [
                    line.split()
                    for line in lines[lines.index(marker) + 1 :]
                    if line.strip()
                ]
                if len(atoms) != len(z):
                    errors.append(label + ": input atom count")
                else:
                    for a, number, point in zip(atoms, z, xyz):
                        if (
                            a[0] != SYMBOLS[number]
                            or math.dist(list(map(float, a[1:])), point) > 1e-8
                        ):
                            errors.append(label + ": input differs from structure")
                            break
            else:
                previous = "opt" if s == "freq" else "freq"
                if "Geom=AllCheck" not in text or f"%OldChk={previous}.chk" not in text:
                    errors.append(label + ": checkpoint linkage")
    checksum = root / "checks/SHA256SUMS.json"
    if checksum.exists():
        for name, digest in json.loads(checksum.read_text()).items():
            f = root / name
            if not f.exists() or sha256(f) != digest:
                errors.append("Checksum mismatch: " + name)
    return errors


def main():
    errors = validate()
    print(
        "\n".join(errors)
        if errors
        else "PASS: 208 jobs, 616 stages; inventory, structures, inputs and checksums."
    )
    raise SystemExit(bool(errors))


if __name__ == "__main__":
    main()
