import csv
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def manifest(root=ROOT):
    with (root / "manifest.csv").open() as f:
        return list(csv.DictReader(f))


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stages(row):
    return ["sp"] if row["kind"] == "ion" else ["opt", "freq", "sp"]


def input_hashes(row, root=ROOT):
    return {s: sha256(root / row["input_dir"] / (s + ".gjf")) for s in stages(row)}


def write_csv(path, rows, fields):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
