import argparse
import json
import math
from collections import Counter

from common import ROOT, config, manifest, runtime_files, sha256, stages

RADII = {
    1: 0.31,
    6: 0.76,
    7: 0.71,
    8: 0.66,
    9: 0.57,
    15: 1.07,
    16: 1.05,
    17: 1.02,
    53: 1.39,
}
SYMBOLS = {1: "H", 6: "C", 7: "N", 8: "O", 9: "F", 15: "P", 16: "S", 17: "Cl", 53: "I"}
PFAS = {
    "PFBA": {6: 4, 9: 7, 8: 2},
    "PFPeA": {6: 5, 9: 9, 8: 2},
    "PFHpA": {6: 7, 9: 13, 8: 2},
    "PFOA": {6: 8, 9: 15, 8: 2},
    "PFNA": {6: 9, 9: 17, 8: 2},
    "PFOS": {6: 8, 9: 17, 8: 3, 16: 1},
}


def structure(path):
    d = json.loads(path.read_text())
    z = d["atoms"]["elements"]["number"]
    flat = d["atoms"]["coords"]["3d"]
    return d, z, [flat[i : i + 3] for i in range(0, len(flat), 3)]


def inspect_structure(d, z, xyz):
    issues = []
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
            issues.append(f"Bond length {a}-{b}: {ratio:.2f} covalent radii")
    seen = set()
    fragments = []
    for i in range(len(z)):
        if i in seen:
            continue
        todo = [i]
        part = set()
        while todo:
            j = todo.pop()
            if j not in part:
                part.add(j)
                todo.extend(adj[j] - part)
        seen |= part
        fragments.append(part)
    for k, first in enumerate(fragments):
        for second in fragments[k + 1 :]:
            if min(math.dist(xyz[i], xyz[j]) for i in first for j in second) < 1.45:
                issues.append("Interfragment clash")
    for a in range(len(z)):
        for b in range(a):
            if b in adj[a]:
                continue
            if math.dist(xyz[a], xyz[b]) < 0.6 * (RADII[z[a]] + RADII[z[b]]):
                issues.append(f"Nonbonded clash {a}-{b}")
    return issues


def validate(root=ROOT, checksums=True):
    issues = []
    try:
        cfg = config(root)
        spec = json.loads((root / "campaign.json").read_text())
        rows = manifest(root)
        assert cfg["modules"] == [
            "devel",
            "python/3.12.1",
            "chemistry",
            "gaussian/g16.B01",
        ]
        assert cfg["partition"] and 1 <= int(cfg["max_parallel"]) <= 8
        assert cfg["temperature_k"] == 298.15 and cfg["entropy_cutoff_cm"] == 100
        assert set(cfg["group_parallel"]) == {"ions", "small", "large"}
        assert all(
            isinstance(v, int) and v >= 1 for v in cfg["group_parallel"].values()
        )
        assert sum(cfg["group_parallel"].values()) <= cfg["max_parallel"]
        assert set(cfg["group_hours"]) == set(cfg["group_parallel"])
        assert all(
            isinstance(v, int) and 1 <= v <= 168 for v in cfg["group_hours"].values()
        )
        assert cfg["failure_threshold"] >= 2
        assert isinstance(cfg["backup_max_gb"], int) and cfg["backup_max_gb"] > 0
        assert isinstance(cfg["account"], str) and isinstance(cfg["qos"], str)
    except (OSError, ValueError, KeyError, AssertionError) as exc:
        return ["Missing/invalid campaign configuration: " + str(exc)]
    if (
        len(rows) != spec["workflows"]
        or sum(len(stages(r)) for r in rows) != spec["stages"]
    ):
        issues.append("Inventory differs from campaign")
    if len({r["job"] for r in rows}) != len(rows):
        issues.append("Duplicate job identifiers")
    paths = set()
    host_formulas = {}
    for resin in spec["resins"]:
        seed = next(
            r for r in rows if r["resin"] == resin and r["kind"] == "counterion"
        )
        _, numbers, _ = structure(root / "structures" / (seed["structure"] + ".cjson"))
        host_formulas[resin] = Counter(n for n in numbers if n not in [9, 17, 53])
    for r in rows:
        try:
            p = root / "structures" / (r["structure"] + ".cjson")
            d, z, xyz = structure(p)
            paths.add(p)
            problems = inspect_structure(d, z, xyz)
            expected_resources = (
                (2, 4, 2, "ions")
                if r["kind"] == "ion"
                else (8, 40, 30, "small")
                if r["kind"] == "free" or r["resin"] == "A400"
                else (8, 64, 48, "large")
            )
            if (
                int(r["cpus"]),
                int(r["mem_gb"]),
                int(r["gaussian_mem_gb"]),
                r["resource_group"],
            ) != expected_resources:
                problems.append("Resource group mismatch")
            q = d["atoms"]["formalCharges"]
            if len(q) != len(z) or sum(q) != int(r["charge"]) or (sum(z) - sum(q)) % 2:
                problems.append("Charge/electron parity")
            if sum(q) != (-1 if r["kind"] in ["free", "ion"] else 0):
                problems.append("Wrong total charge")
            if len(z) != int(r["atoms"]) or int(r["multiplicity"]) != 1:
                problems.append("Atom count/spin")
            if r["kind"] == "free" and Counter(z) != Counter(PFAS[r["pfas"]]):
                problems.append("PFAS formula")
            if r["kind"] == "complex":
                expected = host_formulas[r["resin"]] + Counter(PFAS[r["pfas"]])
                if r["spectator"]:
                    expected.update([{"Cl": 17, "I": 53}[r["spectator"]]])
                if Counter(z) != expected:
                    problems.append("Complex stoichiometry")
            if r["kind"] == "counterion":
                expected = host_formulas[r["resin"]].copy()
                expected.update(
                    {
                        "Cl": [17],
                        "F": [9],
                        "ClCl": [17, 17],
                        "ClI": [17, 53],
                        "II": [53, 53],
                    }[r["state"]]
                )
                if Counter(z) != expected:
                    problems.append("Counterion reference stoichiometry")
            if r["resin"] == "PC4P":
                pq = [charge for n, charge in zip(z, q) if n == 15]
                if sorted(pq) != [0, 1]:
                    problems.append("PC4P phosphorus charges")
            if r["resin"] == "PC4P-Me":
                if [charge for n, charge in zip(z, q) if n == 15] != [1, 1]:
                    problems.append("PC4P-Me phosphorus charges")
                actual = sorted(SYMBOLS[n] for n in z if n in [17, 53])
                expected = sorted(
                    {"ClCl": ["Cl", "Cl"], "ClI": ["Cl", "I"], "II": ["I", "I"]}[
                        r["state"]
                    ]
                    if r["kind"] == "counterion"
                    else [r["spectator"]]
                )
                if actual != expected:
                    problems.append("Wrong counterions")
            for stage in stages(r):
                path = root / r["input_dir"] / (stage + ".gjf")
                paths.add(path)
                text = path.read_text()
                m = spec["methods"][stage]
                route = text.split("#p ", 1)[1].split("\n\n", 1)[0]
                if m["functional"] + "/" not in route:
                    problems.append("Functional " + stage)
                if ("EmpiricalDispersion=GD3BJ" in route) != m["dispersion"]:
                    problems.append("Dispersion " + stage)
                if ("SCRF=(SMD,Solvent=Water)" in route) != (
                    r["environment"] == "water"
                ):
                    problems.append("Solvent " + stage)
                if ("GenECP" in route) != (53 in z):
                    problems.append("ECP " + stage)
                if 53 in z and "I-ECP     3     28" not in text:
                    problems.append("Missing iodine ECP block " + stage)
                for value in [
                    f"%NProcShared={r['cpus']}",
                    f"%Mem={r['gaussian_mem_gb']}GB",
                    "Integral=UltraFine",
                    "SCF=(XQC,Tight)",
                    "5D 7F",
                ]:
                    if value not in text:
                        problems.append("Missing " + value)
                if stage != "opt" and r["kind"] != "ion":
                    previous = "opt" if stage == "freq" else "freq"
                    if (
                        "Geom=AllCheck" not in route
                        or f"%OldChk={previous}.chk" not in text
                    ):
                        problems.append("Checkpoint linkage " + stage)
                else:
                    marker = f"\n{r['charge']} 1\n"
                    atomlines = text.split(marker)[1].split("\n\n")[0].splitlines()
                    if len(atomlines) != len(z):
                        problems.append("Input atom count " + stage)
                    else:
                        for line, n, coord in zip(atomlines, z, xyz):
                            v = line.split()
                            if (
                                v[0] != SYMBOLS[n]
                                or math.dist(list(map(float, v[1:])), coord) > 1e-8
                            ):
                                problems.append("Input coordinates " + stage)
                                break
            issues.extend(r["job"] + ": " + s for s in problems)
        except (ValueError, KeyError, IndexError, OSError) as exc:
            issues.append(r["job"] + ": " + str(exc))
    actual = {
        p
        for folder in ["structures", "inputs"]
        for p in (root / folder).rglob("*")
        if p.is_file()
    }
    if actual != paths:
        issues.append("Unlisted or missing structures/inputs")
    matrix = Counter(
        (r["resin"], r["pfas"], r["environment"], r["site"], r["spectator"])
        for r in rows
        if r["kind"] == "complex"
    )
    if len(matrix) != 96 or any(n != 3 for n in matrix.values()):
        issues.append("Incomplete complex matrix")
    for env in spec["environments"]:
        for pf in spec["pfas"]:
            if (
                sum(
                    r["kind"] == "free" and r["pfas"] == pf and r["environment"] == env
                    for r in rows
                )
                != 2
            ):
                issues.append("Incomplete free references")
        for resin, states in spec["counterion_states"].items():
            for state in states:
                if (
                    sum(
                        r["kind"] == "counterion"
                        and r["resin"] == resin
                        and r["state"] == state
                        and r["environment"] == env
                        for r in rows
                    )
                    != 2
                ):
                    issues.append("Incomplete counterion references")
    if checksums:
        try:
            recorded = json.loads((root / "checks/SHA256SUMS.json").read_text())
            current = {str(p.relative_to(root)): sha256(p) for p in runtime_files(root)}
            if current != recorded:
                issues.append("Runtime checksum inventory mismatch")
        except (OSError, ValueError):
            issues.append("Missing checksum inventory")
    return issues


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--refresh-config", action="store_true")
    args = parser.parse_args()
    if args.refresh_config:
        path = ROOT / "checks/SHA256SUMS.json"
        saved = json.loads(path.read_text())
        current = {str(p.relative_to(ROOT)): sha256(p) for p in runtime_files()}
        if {k: v for k, v in saved.items() if k != "sherlock.json"} != {
            k: v for k, v in current.items() if k != "sherlock.json"
        }:
            raise SystemExit(
                "Files other than sherlock.json changed; configuration refresh refused"
            )
        issues = validate(checksums=False)
        if issues:
            raise SystemExit("\n".join(issues))
        path.write_text(json.dumps(current, indent=2) + "\n")
    errors = validate()
    print(
        "\n".join(errors)
        if errors
        else "PASS: configuration, inventory, structures, inputs and runtime checksums"
    )
    raise SystemExit(bool(errors))
