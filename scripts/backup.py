import argparse
import fcntl
import json
import os
import re
import subprocess
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


def quota_bytes(text):
    match = re.search(
        r"^\s*GROUP_HOME\s*\|\s*([0-9.]+)([KMGT]?B)\s*/\s*([0-9.]+)([KMGT]?B)",
        text,
        re.M,
    )
    if not match:
        raise RuntimeError("Group quota could not be verified")
    factors = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    used = float(match[1]) * factors[match[2]]
    limit = float(match[3]) * factors[match[4]]
    return max(0, limit - used - factors[match[2]])


def storage_status(root=ROOT, new_start=False):
    target = destination(root)
    target.mkdir(parents=True, exist_ok=True)
    objects = target / "objects"
    usage = (
        sum(p.stat().st_size for p in objects.iterdir() if p.is_file())
        if objects.exists()
        else 0
    )
    cfg = config(root)
    quota = subprocess.run(
        ["sh_quota"], capture_output=True, text=True, check=True, timeout=30
    )
    free = min(quota_bytes(quota.stdout), shutil.disk_usage(target).free)
    status = {
        "used_bytes": usage,
        "group_available_bytes": free,
        "warning": usage >= cfg.get("backup_warn_gb", 80) * 1024**3,
    }
    if new_start and (
        usage >= cfg.get("backup_stop_gb", 90) * 1024**3 or free < 2 * 1024**3
    ):
        raise RuntimeError("Backup capacity blocks new calculations")
    return status


def verify_snapshot(record, required=None):
    record = Path(record)
    data = json.loads(record.read_text())
    objects = record.parents[3] / "objects"
    for name, digest in data["files"].items():
        if required is not None and name not in required:
            continue
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or sha256(objects / digest) != digest
        ):
            raise RuntimeError("Stored backup checksum mismatch: " + name)
    if required and not set(required) <= set(data["files"]):
        raise RuntimeError("Backup manifest omits required outputs")
    return data


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
        available = storage_status(root)["group_available_bytes"]
        needed = sum(
            source.stat().st_size
            for name, source in files.items()
            if not (objects / mapping[name]).exists()
        )
        if usage + needed > limit or available < needed + 1024**3:
            raise RuntimeError("Insufficient verified backup capacity")
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
