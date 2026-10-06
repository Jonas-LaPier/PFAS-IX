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


def terminal_failure(text, returncode=None):
    normal = text.rfind("Normal termination of Gaussian")
    fatal = list(
        re.finditer(
            r"Error termination[^\n]*|^ntrbks[^\n]*|Erroneous write[^\n]*|"
            r"galloc:[^\n]*|No space left on device[^\n]*|Input/output error[^\n]*|"
            r"Transport endpoint is not connected[^\n]*",
            text,
            re.MULTILINE,
        )
    )
    if normal >= 0 and not any(m.start() > normal for m in fatal) and not returncode:
        return None
    end = fatal[-1].end() if fatal else len(text)
    tail = text[max(0, normal) : end]
    patterns = {
        "file_access": r"ntrbks|Erroneous write|No space left on device|Input/output error|Transport endpoint is not connected|Error opening|Permission denied|Resource temporarily unavailable",
        "memory": r"galloc:|out of memory",
        "pcm": r"Inv3 failed in PCMMkU",
        "scf": r"Convergence failure|No lower point found",
        "steps": r"Number of steps exceeded",
        "setup": r"QPErr|command not found",
    }
    events = [
        (m.start(), kind)
        for kind, pattern in patterns.items()
        for m in re.finditer(pattern, tail, re.I)
    ]
    if events:
        return max(events)[1]
    if "l508.exe" in tail:
        return "scf"
    return "unfinished" if normal < 0 else "unknown"


def check(path, stage, row):
    text = Path(path).read_text(errors="replace") if Path(path).exists() else ""
    issues = []
    if text.count("Normal termination of Gaussian") != 1:
        issues.append("Expected exactly one normal termination")
    if terminal_failure(text) not in (None, "unfinished"):
        issues.append("Gaussian error marker")
    tail = text.rsplit("Normal termination of Gaussian", 1)[-1]
    if text and ("SCF Done:" in tail or "Entering Gaussian" in tail):
        issues.append("Trailing unfinished calculation")
    energies = re.findall(r"SCF Done:\s+E\(([^)]+)\)\s*=\s*(" + NUMBER + ")", text)
    method = (
        "M062X"
        if stage == "method"
        else "PBE1PBE" if stage in ("sp", "basis") else "PBEPBE"
    )
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
        if any(not math.isfinite(v) for v in frequencies):
            issues.append("Nonfinite vibrational frequency")
        if any(v <= 0 for v in frequencies):
            issues.append("Imaginary frequencies; minimum not accepted")
    route_blocks = re.findall(r"#[pPnN].*?(?=\n\s*-{5,})", text, re.DOTALL)
    route = "".join("".join(route_blocks).upper().split())
    if method + "/GEN" not in route:
        issues.append("Expected functional/general basis route missing")
    if ("GD3BJ" in route) != (stage != "method"):
        issues.append("Dispersion route mismatch")
    if ("SCRF=(SMD,SOLVENT=WATER)" in route) != (row["environment"] == "water"):
        issues.append("Solvent route mismatch")
    if "5D7F" not in route:
        issues.append("Spherical basis convention mismatch")
    if "INTEGRAL=ULTRAFINE" not in route:
        issues.append("Integration grid mismatch")
    if not re.search(r"(?:G16Rev|Revision\s+)B\.01", text):
        issues.append("Gaussian revision differs from B.01")
    thermal = re.findall(
        r"Thermal correction to Gibbs Free Energy=\s*(" + NUMBER + ")", text
    )
    temperatures = re.findall(r"Temperature\s+(" + NUMBER + r")\s+Kelvin", text)
    if stage == "freq":
        if not thermal:
            issues.append("Missing Gibbs thermal correction")
        if not temperatures or abs(number(temperatures[-1]) - 298.15) > 0.01:
            issues.append("Thermochemistry temperature mismatch")
    geometry = last_geometry(text)
    if any(not math.isfinite(v) for atom in geometry for v in atom[1:]):
        issues.append("Nonfinite geometry")
    if thermal and not math.isfinite(number(thermal[-1])):
        issues.append("Nonfinite thermal correction")
    if row["kind"] != "ion" and len(geometry) != int(row["atoms"]):
        issues.append("Final geometry missing or atom count mismatched")
    return {
        "valid": not issues,
        "issues": issues,
        "energy_hartree": energy,
        "imaginary_frequencies": sum(v < 0 for v in frequencies),
        "lowest_frequency": min(frequencies) if frequencies else None,
        "geometry": geometry,
        "frequencies_cm": frequencies,
        "thermal_g_hartree": number(thermal[-1]) if thermal else None,
    }


def main():
    p = argparse.ArgumentParser(description=None)
    p.add_argument("job")
    p.add_argument("stage", choices=["opt", "freq", "sp", "method", "basis"])
    p.add_argument("log")
    a = p.parse_args()
    row = next(r for r in manifest() if r["job"] == a.job)
    result = check(a.log, a.stage, row)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["valid"] else 1)


if __name__ == "__main__":
    main()
