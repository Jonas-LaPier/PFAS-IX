#!/usr/bin/env python3


import argparse
import json
import math
import re
from pathlib import Path

from common import manifest

NUMBER = r"[-+]?\d+(?:\.\d*)?(?:[DEde][-+]?\d+)?"


def number(value):
    return float(value.replace("D", "E").replace("d", "e"))


def last_geometry(text):
    blocks = re.findall(
        r"(?:Standard|Input) orientation:.*?\n\s*-+\n.*?\n\s*-+\n(.*?)\n\s*-+",
        text,
        re.DOTALL,
    )
    if not blocks:
        return []
    atoms = []
    for line in blocks[-1].splitlines():
        x = line.split()
        if len(x) == 6:
            atoms.append([int(x[1]), *[number(v) for v in x[3:]]])
    return atoms


def check(path, stage, row):
    text = Path(path).read_text(errors="replace") if Path(path).exists() else ""
    issues = []
    if text.count("Normal termination of Gaussian") != 1:
        issues.append("Expected exactly one normal termination")
    if any(
        x in text
        for x in [
            "Error termination",
            "Convergence failure",
            "Erroneous write",
            "galloc:",
        ]
    ):
        issues.append("Gaussian error marker")
    tail = text.rsplit("Normal termination of Gaussian", 1)[-1]
    if text and ("SCF Done:" in tail or "Entering Gaussian" in tail):
        issues.append("Trailing unfinished calculation")
    energies = re.findall(r"SCF Done:\s+E\(([^)]+)\)\s*=\s*(" + NUMBER + ")", text)
    method = "PBE1PBE" if stage == "sp" else "PBEPBE"
    energy = None
    if not energies or method not in energies[-1][0].upper().replace("-", ""):
        issues.append("Missing expected " + method + " energy")
    else:
        energy = number(energies[-1][1])
        if not math.isfinite(energy):
            issues.append("Nonfinite energy")
    charge = re.findall(r"Charge\s*=\s*(-?\d+)\s+Multiplicity\s*=\s*(\d+)", text)
    if not charge or tuple(map(int, charge[-1])) != (int(row["charge"]), 1):
        issues.append("Charge/multiplicity missing or mismatched")
    if stage == "opt" and "Optimization completed" not in text:
        issues.append("Optimization not completed")
    frequencies = [
        number(v)
        for line in re.findall(r"Frequencies --([^\n]+)", text)
        for v in line.split()
    ]
    if stage == "freq":
        if len(frequencies) != 3 * int(row["atoms"]) - 6:
            issues.append("Incomplete vibrational spectrum")
        if any(v < 0 for v in frequencies):
            issues.append("Imaginary frequencies; minimum not accepted")
    geometry = last_geometry(text)
    if row["kind"] != "ion" and len(geometry) != int(row["atoms"]):
        issues.append("Final geometry missing or atom count mismatched")
    return {
        "valid": not issues,
        "issues": issues,
        "energy_hartree": energy,
        "imaginary_frequencies": sum(v < 0 for v in frequencies),
        "lowest_frequency": min(frequencies) if frequencies else None,
        "geometry": geometry,
    }


def main():
    p = argparse.ArgumentParser(description=None)
    p.add_argument("job")
    p.add_argument("stage", choices=["opt", "freq", "sp"])
    p.add_argument("log")
    a = p.parse_args()
    row = next(r for r in manifest() if r["job"] == a.job)
    result = check(a.log, a.stage, row)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
