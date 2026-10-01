import argparse
import fcntl
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from common import ROOT, config, runtime_files, sha256, write_json


def destination(root=ROOT):
    group = os.environ.get("GROUP_HOME")
    user = os.environ.get("USER")
    if not group or not user or Path(user).name != user:
        raise RuntimeError("Backup requires GROUP_HOME and USER")
    parent = Path(group).resolve(strict=True)
    if not parent.is_dir() or not os.access(parent, os.W_OK):
        raise RuntimeError("Group home is not writable")
    scratch = os.environ.get("SCRATCH")
    if scratch and parent.is_relative_to(Path(scratch).resolve()):
        raise RuntimeError("Backup destination cannot be scratch storage")
    target = parent / user / "PFAS-IX-backup"
    if not target.resolve().is_relative_to(parent):
        raise RuntimeError("Backup destination escapes group home")
    return target


def snapshot(attempt, receipt, root=ROOT):
    target = destination(root)
    target.mkdir(parents=True, exist_ok=True)
    with (target / "backup.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        objects = target / "objects"
        objects.mkdir(exist_ok=True)
        files = {"package/" + str(p.relative_to(root)): p for p in runtime_files(root)}
        files["package/checks/SHA256SUMS.json"] = root / "checks/SHA256SUMS.json"
        files["receipt.json"] = receipt
        files.update(
            {
                "attempt/" + p.name: p
                for p in attempt.iterdir()
                if p.is_file()
                and p.suffix in [".gjf", ".log", ".chk", ".fchk", ".json"]
                and p.name != "backup.json"
            }
        )
        mapping = {name: sha256(p) for name, p in files.items()}
        usage = sum(p.stat().st_size for p in objects.iterdir() if p.is_file())
        limit = config(root)["backup_max_gb"] * 1024**3
        for name, source in files.items():
            saved = objects / mapping[name]
            if saved.exists():
                if sha256(saved) != mapping[name]:
                    raise RuntimeError("Existing backup is corrupt: " + str(saved))
                continue
            size = source.stat().st_size
            if usage + size > limit or shutil.disk_usage(target).free < size + 1024**3:
                raise RuntimeError(
                    "Backup space limit reached; increase storage before continuing"
                )
            with tempfile.NamedTemporaryFile(
                dir=objects, prefix="partial-", delete=False
            ) as stream:
                temp = Path(stream.name)
                try:
                    with source.open("rb") as inp:
                        shutil.copyfileobj(inp, stream, 1024 * 1024)
                    stream.flush()
                    os.fsync(stream.fileno())
                    if sha256(temp) != mapping[name]:
                        raise RuntimeError(
                            "Backup checksum mismatch or source changed: " + name
                        )
                    temp.replace(saved)
                finally:
                    temp.unlink(missing_ok=True)
            usage += size
        folders = target / "snapshots" / attempt.parent.name / attempt.name
        folders.mkdir(parents=True, exist_ok=True)
        record = folders / (str(time.time_ns()) + ".json")
        write_json(
            record, {"files": mapping, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
        )
        return str(record)


def restore(record, output):
    data = json.loads(record.read_text())
    objects = record.parents[3] / "objects"
    output.mkdir(parents=True, exist_ok=False)
    for name, digest in data["files"].items():
        dest = output / name
        if (
            not dest.resolve().is_relative_to(output.resolve())
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise RuntimeError("Invalid backup manifest")
        source = objects / digest
        if sha256(source) != digest:
            raise RuntimeError("Backup checksum mismatch: " + name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        if sha256(dest) != digest:
            raise RuntimeError("Restore checksum mismatch: " + name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--restore", nargs=2, type=Path, metavar=("MANIFEST", "DESTINATION")
    )
    args = parser.parse_args()
    if args.check:
        print("Backup destination:", destination())
    elif args.restore:
        restore(*args.restore)
    else:
        parser.error("Use --check or --restore MANIFEST DESTINATION")
