#!/usr/bin/env python3


import json
import math

from check_log import check
from common import ROOT, input_hashes, manifest, sha256, stages, write_csv
from validate import inspect_structure, structure

HARTREE_TO_KJ = 2625.4996394799


def inspect_attempt(folder, row, root=ROOT):
    issues = []
    meta_path = folder / "run.json"
    if not meta_path.exists():
        return None, ["Missing run provenance"]
    meta = json.loads(meta_path.read_text())
    if meta.get("status") != "complete":
        issues.append("Workflow not complete")
    if meta.get("job") != row["job"]:
        issues.append("Wrong job provenance")
    if meta.get("input_hashes") != input_hashes(row, root):
        issues.append("Inputs changed or provenance mismatched")
    energy = None
    geometry = []
    for stage in stages(row):
        log = folder / (stage + ".log")
        result = check(log, stage, row)
        issues.extend(stage + ": " + s for s in result["issues"])
        if log.exists() and meta.get("log_hashes", {}).get(stage) != sha256(log):
            issues.append(stage + ": log checksum mismatch")
        if stage == "sp":
            energy = result["energy_hartree"]
            geometry = result["geometry"]
    if row["kind"] != "ion" and len(geometry) == int(row["atoms"]):
        d, z, xyz = structure(root / "structures" / (row["structure"] + ".cjson"))
        if [a[0] for a in geometry] != z:
            issues.append("Final atom identities/order differ")
        else:
            final = [a[1:] for a in geometry]
            issues.extend(
                "Geometry review: " + s for s in inspect_structure(d, z, final)
            )

            if row["kind"] == "complex":
                center = d["properties"]["active_center"]
                oxygens = [i for i, n in enumerate(z) if n == 8]
                distance = min(math.dist(final[center], final[i]) for i in oxygens)
                if distance > 5.5:
                    issues.append(
                        f"Geometry review: PFAS contact {distance:.2f} A; possible dissociation"
                    )
    return energy, issues


def collect(root=ROOT):
    accepted = {}
    audit = []
    for row in manifest(root):
        folder = root / "results" / row["job"]
        candidates = []
        for attempt in sorted(folder.glob("*")) if folder.exists() else []:
            if not attempt.is_dir():
                continue
            try:
                energy, issues = inspect_attempt(attempt, row, root)
            except (OSError, ValueError, KeyError) as exc:
                energy, issues = None, ["Unreadable/incomplete attempt: " + str(exc)]
            audit.append(
                {
                    "job": row["job"],
                    "attempt": attempt.name,
                    "status": "review" if issues else "valid",
                    "issues": "; ".join(issues),
                    "energy_hartree": energy,
                }
            )
            if not issues:
                candidates.append((attempt, energy))
        if len(candidates) == 1:
            accepted[row["job"]] = candidates[0][1]
        elif len(candidates) > 1:
            audit.append(
                {
                    "job": row["job"],
                    "attempt": "",
                    "status": "ambiguous",
                    "issues": "Multiple valid attempts; explicitly resolve which to retain before analysis",
                    "energy_hartree": "",
                }
            )
        elif not folder.exists():
            audit.append(
                {
                    "job": row["job"],
                    "attempt": "",
                    "status": "missing",
                    "issues": "Not run",
                    "energy_hartree": "",
                }
            )
    return accepted, audit


def exchange_rows(rows, energies):
    output = []
    for r in rows:
        if r["kind"] != "complex":
            continue
        for counterion in ["Cl", "F"] if r["resin"] == "A400" else ["Cl"]:
            env = r["environment"]
            keys = [
                r["job"],
                f"free_{counterion}__{env}",
                f"{r['resin']}_{counterion}__{env}",
                f"free_{r['pfas']}__{env}",
            ]
            missing = [k for k in keys if k not in energies]
            delta = (
                None
                if missing
                else energies[keys[0]]
                + energies[keys[1]]
                - energies[keys[2]]
                - energies[keys[3]]
            )
            output.append(
                {
                    "resin": r["resin"],
                    "pfas": r["pfas"],
                    "environment": env,
                    "counterion": counterion,
                    "pose": r["pose"],
                    "status": "missing_or_rejected" if missing else "valid",
                    "delta_E_hartree": "" if delta is None else delta,
                    "delta_E_kJ_mol": "" if delta is None else delta * HARTREE_TO_KJ,
                    "missing": "; ".join(missing),
                }
            )
    return output


def main():
    accepted, audit = collect()
    output = exchange_rows(manifest(), accepted)
    dest = ROOT / "results"
    dest.mkdir(exist_ok=True)
    write_csv(
        dest / "status.csv",
        audit,
        ["job", "attempt", "status", "issues", "energy_hartree"],
    )
    write_csv(dest / "exchange_energies.csv", output, list(output[0]))
    groups = {}
    for r in output:
        groups.setdefault(
            tuple(r[k] for k in ["resin", "pfas", "environment", "counterion"]), []
        ).append(r)
    summary = []
    for key, rows in groups.items():
        good = [r for r in rows if r["status"] == "valid"]
        best = min(good, key=lambda r: r["delta_E_hartree"]) if good else None
        summary.append(
            dict(
                zip(["resin", "pfas", "environment", "counterion"], key),
                valid_starting_poses=len(good),
                status="complete_3_poses" if len(good) == 3 else "incomplete",
                lowest_pose=best["pose"] if best else "",
                lowest_delta_E_kJ_mol=best["delta_E_kJ_mol"] if best else "",
            )
        )
    write_csv(dest / "lowest_pose_energies.csv", summary, list(summary[0]))
    print(
        f"{len(accepted)}/208 workflows accepted. {sum(r['status'] == 'valid' for r in output)}/216 exchange cycles available."
    )
    print(
        "Lowest-pose energies are not conformational free energies. Inspect optimized geometries; equivalent poses are not independent observations."
    )


if __name__ == "__main__":
    main()
