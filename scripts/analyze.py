import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from check_log import check
from common import ROOT, config, input_hashes, manifest, sha256, stages, write_csv
from validate import RADII, inspect_structure, structure, validate

HARTREE_TO_KJ = 2625.4996394799
R = 8.31446261815324
KB = 1.380649e-23
H = 6.62607015e-34
NA = 6.02214076e23
MASSES = {"F": 18.998403163, "Cl": 34.968852682, "I": 126.9044719}


def vibrational_entropy(frequency, temperature):
    x = 1.438776877 * frequency / temperature
    if x > 700:
        return 0.0
    return R * (x / math.expm1(x) - math.log(-math.expm1(-x)))


def thermal_correction(row, frequency_result, cfg):
    t = cfg["temperature_k"]
    if row["kind"] == "ion":
        mass = MASSES[row["state"]] * 1.66053906660e-27
        volume = 1 / (1000 * NA)
        entropy = R * (
            math.log((2 * math.pi * mass * KB * t / H**2) ** 1.5 * volume) + 2.5
        )
        return (2.5 * R * t - t * entropy) / (HARTREE_TO_KJ * 1000)
    correction = frequency_result["thermal_g_hartree"]
    if correction is None:
        raise ValueError("Missing thermal correction")
    delta_s = sum(
        vibrational_entropy(v, t)
        - vibrational_entropy(max(v, cfg["entropy_cutoff_cm"]), t)
        for v in frequency_result["frequencies_cm"]
    )
    standard = R * t * math.log(1000 * R * t / 101325)
    return correction + (t * delta_s + standard) / (HARTREE_TO_KJ * 1000)


def geometry_distance(first, second):
    heavy = [i for i, a in enumerate(first) if a[0] != 1]
    values = [
        (
            math.dist(first[a][1:], first[b][1:])
            - math.dist(second[a][1:], second[b][1:])
        )
        ** 2
        for k, a in enumerate(heavy)
        for b in heavy[:k]
    ]
    return math.sqrt(sum(values) / len(values)) if values else 0.0


def same_geometry(first, second):
    if not first or [a[0] for a in first] != [a[0] for a in second]:
        return False
    if any(
        not math.isfinite(v) for atoms in [first, second] for a in atoms for v in a[1:]
    ):
        return False
    return all(
        abs(
            math.dist(first[a][1:], first[b][1:])
            - math.dist(second[a][1:], second[b][1:])
        )
        < 0.00005
        for a in range(len(first))
        for b in range(a)
    )


def inspect_attempt(folder, row, root=ROOT):
    meta = json.loads((folder / "run.json").read_text())
    issues = []
    results = {}
    if meta.get("job") != row["job"]:
        issues.append("Wrong job provenance")
    if meta.get("input_hashes") != input_hashes(row, root):
        issues.append("Input provenance mismatch")
    for stage in stages(row):
        log = folder / (stage + ".log")
        result = check(log, stage, row)
        results[stage] = result
        if log.exists() and meta.get("log_hashes", {}).get(stage) != sha256(log):
            result["issues"].append("Log checksum mismatch")
            result["valid"] = False
        executed = meta.get("executed_input_hashes", {}).get(stage)
        if executed and (
            not (folder / (stage + ".gjf")).exists()
            or sha256(folder / (stage + ".gjf")) != executed
        ):
            result["issues"].append("Executed input checksum mismatch")
            result["valid"] = False
    if row["kind"] != "ion":
        for stage in stages(row):
            if (
                stage != "opt"
                and results[stage]["valid"]
                and not same_geometry(
                    results["opt"]["geometry"], results[stage]["geometry"]
                )
            ):
                results[stage]["issues"].append(
                    "Geometry differs from optimized structure"
                )
                results[stage]["valid"] = False
    core = ["sp"] if row["kind"] == "ion" else ["opt", "freq", "sp"]
    for stage in core:
        issues.extend(stage + ": " + s for s in results[stage]["issues"])
    geometry = results["sp"]["geometry"]
    nearest = ""
    if row["kind"] != "ion" and len(geometry) == int(row["atoms"]):
        d, z, _ = structure(root / "structures" / (row["structure"] + ".cjson"))
        if [a[0] for a in geometry] != z:
            issues.append("Atom order/identity changed")
        else:
            final = [a[1:] for a in geometry]
            issues.extend(inspect_structure(d, z, final))
            bonds = d["bonds"]["connections"]["index"]
            bonded = {tuple(sorted((a, b))) for a, b in zip(bonds[::2], bonds[1::2])}
            for a in range(len(z)):
                for b in range(a):
                    if (b, a) not in bonded and math.dist(final[a], final[b]) < 1.12 * (
                        RADII[z[a]] + RADII[z[b]]
                    ):
                        issues.append(
                            f"Possible new covalent bond {a}-{b}; inspect geometry"
                        )
            charged_centers = [
                i
                for i, (n, q) in enumerate(zip(z, d["atoms"]["formalCharges"]))
                if n in [7, 15] and q == 1
            ]
            for halide in [
                i
                for i, n in enumerate(z)
                if n in [17, 53] or (n == 9 and row["kind"] == "counterion")
            ]:
                if (
                    charged_centers
                    and min(math.dist(final[halide], final[c]) for c in charged_centers)
                    > 6.0
                ):
                    issues.append("Dissociated counterion; inspect as physical outcome")
            if row["kind"] == "complex":
                centers = [
                    i
                    for i, (n, q) in enumerate(zip(z, d["atoms"]["formalCharges"]))
                    if n in [7, 15] and q == 1
                ]
                oxygens = [i for i, n in enumerate(z) if n == 8]
                distance, nearest = min(
                    (math.dist(final[c], final[o]), c) for c in centers for o in oxygens
                )
                if distance > 5.5:
                    issues.append("Dissociated PFAS: inspect as physical outcome")
    values = {}
    if not issues:
        thermal = thermal_correction(row, results.get("freq", {}), config(root))
        for stage in ["sp", "method", "basis"]:
            if stage in results and results[stage]["valid"]:
                values[stage] = {
                    "E": results[stage]["energy_hartree"],
                    "G": results[stage]["energy_hartree"] + thermal,
                }
    return values, issues, geometry, nearest, results


def collect(root=ROOT, selections=None):
    accepted = {}
    audit = []
    geometries = {}
    for row in manifest(root):
        parent = root / "results" / row["job"]
        good = []
        for folder in sorted(parent.iterdir()) if parent.exists() else []:
            if not folder.is_dir():
                continue
            try:
                values, issues, geometry, nearest, results = inspect_attempt(
                    folder, row, root
                )
            except (OSError, ValueError, KeyError, IndexError) as exc:
                values = {}
                issues = [str(exc)]
                geometry = []
                nearest = ""
                results = {}
            notes = []
            if nearest != "" and str(nearest) != row["site"]:
                notes.append("PFAS migrated to site " + str(nearest))
            for stage in ["method", "basis"]:
                if stage in results and not results[stage]["valid"]:
                    notes.append(
                        stage + " unavailable: " + "; ".join(results[stage]["issues"])
                    )
            audit.append(
                {
                    "job": row["job"],
                    "attempt": folder.name,
                    "status": "review" if issues else "valid",
                    "issues": "; ".join(issues),
                    "notes": "; ".join(notes),
                }
            )
            if values:
                good.append((folder.name, values, geometry))
        if selections and row["job"] in selections:
            good = [x for x in good if x[0] == selections[row["job"]]]
        if len(good) == 1:
            accepted[row["job"]] = good[0][1]
            geometries[row["job"]] = good[0][2]
        elif len(good) > 1:
            audit.append(
                {
                    "job": row["job"],
                    "attempt": "",
                    "status": "ambiguous",
                    "issues": "Select one accepted attempt with --selections",
                    "notes": "",
                }
            )
        elif not parent.exists():
            audit.append(
                {
                    "job": row["job"],
                    "attempt": "",
                    "status": "missing",
                    "issues": "Not run",
                    "notes": "",
                }
            )
    return accepted, audit, geometries


def reference_choices(row):
    if row["resin"] == "PC4P-Me":
        return (
            [("ClCl", "Cl"), ("ClI", "I")]
            if row["spectator"] == "Cl"
            else [("ClI", "Cl"), ("II", "I")]
        )
    return [("Cl", "Cl"), ("F", "F")] if row["resin"] == "A400" else [("Cl", "Cl")]


def exchange_rows(rows, energies):
    output = []
    for row in rows:
        if row["kind"] != "complex":
            continue
        for state, leaving in reference_choices(row):
            ref = [
                r
                for r in rows
                if r["kind"] == "counterion"
                and r["resin"] == row["resin"]
                and r["state"] == state
                and r["environment"] == row["environment"]
            ]
            free = [
                r
                for r in rows
                if r["kind"] == "free"
                and r["pfas"] == row["pfas"]
                and r["environment"] == row["environment"]
            ]
            ions = [
                r
                for r in rows
                if r["kind"] == "ion"
                and r["state"] == leaving
                and r["environment"] == row["environment"]
            ]
            for method in ["sp", "method", "basis"]:
                groups = [[row], ref, free, ions]
                if not all(
                    group and all(method in stages(r) for r in group)
                    for group in groups
                ):
                    continue
                pools = [
                    [r for r in group if method in energies.get(r["job"], {})]
                    for group in groups
                ]
                missing = [
                    r["job"]
                    for group, pool in zip(groups, pools)
                    for r in group
                    if r not in pool
                ]
                record = {
                    "resin": row["resin"],
                    "pfas": row["pfas"],
                    "environment": row["environment"],
                    "initial_counterions": state,
                    "leaving_ion": leaving,
                    "spectator": row["spectator"],
                    "starting_site": row["site"],
                    "job": row["job"],
                    "method": method,
                    "status": "incomplete" if missing else "complete",
                    "missing": ";".join(missing),
                }
                for metric in ["E", "G"]:
                    if all(pools):
                        selected = [
                            min(pool, key=lambda r: energies[r["job"]][method][metric])
                            for pool in pools
                        ]
                        values = [energies[r["job"]][method][metric] for r in selected]
                        record["delta_" + metric + "_kJ_mol"] = (
                            values[0] + values[3] - values[1] - values[2]
                        ) * HARTREE_TO_KJ
                        record[metric + "_reference_jobs"] = ";".join(
                            r["job"] for r in selected[1:]
                        )
                    else:
                        record["delta_" + metric + "_kJ_mol"] = ""
                        record[metric + "_reference_jobs"] = ""
                output.append(record)
    return output


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--selections", type=Path)
    a = p.parse_args()
    problems = validate()
    if problems:
        raise SystemExit("Package validation failed: " + "; ".join(problems))
    selections = json.loads(a.selections.read_text()) if a.selections else None
    accepted, audit, geometries = collect(selections=selections)
    rows = manifest()
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    write_csv(
        out / "status.csv", audit, ["job", "attempt", "status", "issues", "notes"]
    )
    cycles = exchange_rows(rows, accepted)
    if cycles:
        write_csv(out / "exchange_energies.csv", cycles, list(cycles[0]))
    groups = defaultdict(list)
    for r in cycles:
        groups[
            tuple(
                r[k]
                for k in [
                    "resin",
                    "pfas",
                    "environment",
                    "initial_counterions",
                    "leaving_ion",
                    "spectator",
                    "method",
                ]
            )
        ].append(r)
    summary = []
    for key, group in groups.items():
        record = dict(
            zip(
                [
                    "resin",
                    "pfas",
                    "environment",
                    "initial_counterions",
                    "leaving_ion",
                    "spectator",
                    "method",
                ],
                key,
            )
        )
        record["status"] = (
            "complete"
            if all(r["status"] == "complete" for r in group)
            else "incomplete"
        )
        record["expected_complexes"] = len(group)
        record["accepted_complexes"] = sum(r["delta_E_kJ_mol"] != "" for r in group)
        for metric in ["E", "G"]:
            valid = [r for r in group if r["delta_" + metric + "_kJ_mol"] != ""]
            best = (
                min(valid, key=lambda r: r["delta_" + metric + "_kJ_mol"])
                if valid
                else None
            )
            record["lowest_delta_" + metric + "_kJ_mol"] = (
                best["delta_" + metric + "_kJ_mol"] if best else ""
            )
            record["lowest_" + metric + "_job"] = best["job"] if best else ""
        summary.append(record)
    if summary:
        write_csv(out / "exchange_summary.csv", summary, list(summary[0]))
    sensitivity = []
    keys = [
        "resin",
        "pfas",
        "environment",
        "initial_counterions",
        "leaving_ion",
        "spectator",
    ]
    baseline = {tuple(r[k] for k in keys): r for r in summary if r["method"] == "sp"}
    for r in summary:
        if r["method"] == "sp":
            continue
        base = baseline[tuple(r[k] for k in keys)]
        item = {k: r[k] for k in keys + ["method"]}
        item["status"] = (
            "complete" if r["status"] == base["status"] == "complete" else "incomplete"
        )
        for metric in ["E", "G"]:
            label = "lowest_delta_" + metric + "_kJ_mol"
            first = r[label]
            second = base[label]
            item[metric + "_change_kJ_mol"] = (
                first - second if first != "" and second != "" else ""
            )
            item[metric + "_exchange_sign_changes"] = (
                str(first * second < 0) if first != "" and second != "" else ""
            )
        sensitivity.append(item)
    if sensitivity:
        write_csv(out / "method_sensitivity.csv", sensitivity, list(sensitivity[0]))
    duplicates = []
    pools = defaultdict(list)
    for r in rows:
        if r["job"] in geometries and geometries[r["job"]]:
            pools[
                tuple(
                    r[k]
                    for k in [
                        "resin",
                        "pfas",
                        "environment",
                        "kind",
                        "state",
                        "spectator",
                    ]
                )
            ].append(r)
    for group in pools.values():
        for i, r in enumerate(group):
            for other in group[:i]:
                g1, g2 = geometries[r["job"]], geometries[other["job"]]
                if [a[0] for a in g1] != [a[0] for a in g2]:
                    continue
                distance = geometry_distance(g1, g2)
                if distance < 0.05:
                    duplicates.append(
                        {
                            "job": r["job"],
                            "same_geometry_as": other["job"],
                            "distance_matrix_rms_A": distance,
                        }
                    )
    write_csv(
        out / "duplicate_minima.csv",
        duplicates,
        ["job", "same_geometry_as", "distance_matrix_rms_A"],
    )
    print(
        f"{len(accepted)}/{len(rows)} core workflows accepted. Inspect incomplete groups, site migration and duplicate minima."
    )
    print(
        "G uses 298.15 K, 1 M, unscaled frequencies and a 100 cm-1 entropy floor. It is a molecular minimum estimate, not a polymer equilibrium constant or conformer ensemble."
    )


if __name__ == "__main__":
    main()
