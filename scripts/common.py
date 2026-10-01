import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def manifest(root=ROOT):
    with (root / "manifest.csv").open() as f:
        return list(csv.DictReader(f))


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stages(row):
    return row["stages"].split(";")


def input_hashes(row, root=ROOT):
    return {s: sha256(root / row["input_dir"] / (s + ".gjf")) for s in stages(row)}


def write_csv(path, rows, fields):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def runtime_files(root=ROOT):
    return sorted(
        [root / x for x in ["manifest.csv", "sherlock.json", "campaign.json"]]
        + [
            p
            for folder in ["structures", "inputs", "scripts"]
            for p in (root / folder).rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
        ]
    )


def config(root=ROOT):
    return json.loads((root / "sherlock.json").read_text())


def write_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.replace(path)
