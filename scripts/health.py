import argparse
import fcntl
import json
import os
import subprocess
from pathlib import Path

from common import config, write_json


def claim(receipt_path, task_id):
    receipt_path = Path(receipt_path)
    task_id = str(int(task_id))
    receipt = json.loads(receipt_path.read_text())
    jobs = receipt["jobs"]
    if not 0 <= int(task_id) < len(jobs) or len(set(jobs)) != len(jobs):
        raise ValueError("Invalid array task or job list")
    folder = receipt_path.parent
    with (folder / "dispatch.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / "dispatch.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"jobs": jobs, "claims": {}}
        )
        if state["jobs"] != jobs:
            raise ValueError("Dispatch job list changed")
        claims = state["claims"]
        if len(set(claims.values())) != len(claims) or not set(claims.values()) <= set(
            jobs
        ):
            raise ValueError("Invalid dispatch claims")
        if task_id not in claims:
            claims[task_id] = next(job for job in jobs if job not in claims.values())
            write_json(path, state)
        return claims[task_id]


def update(receipt_path, job, failure=None):
    receipt = json.loads(Path(receipt_path).read_text())
    folder = Path(receipt.get("health_dir", str(Path(receipt_path).parent)))
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "health.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = folder / "health.json"
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {"failures": {}, "stopped": False}
        )
        if failure:
            state["failures"][job] = failure
            if len(state["failures"]) >= config()["failure_threshold"]:
                state["stopped"] = True
            temp = folder / "health.tmp"
            temp.write_text(json.dumps(state, indent=2) + "\n")
            temp.replace(path)
        return state["stopped"]


def rebalance(receipt_path):
    receipt_path = Path(receipt_path)
    if receipt_path.parent.name != "large":
        return
    folder = receipt_path.parent.parent
    cfg = config()
    with (folder / "rebalance.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = folder / "rebalance.json"
        state = (
            json.loads(record.read_text())
            if record.exists()
            else {"large_cap": cfg["group_parallel"]["large"]}
        )
        if state["large_cap"] >= cfg["max_parallel"]:
            return
        if (folder / "health.json").exists() and json.loads(
            (folder / "health.json").read_text()
        ).get("stopped"):
            return
        receipts = {
            p.parent.name: json.loads(p.read_text())
            for p in folder.glob("*/receipt.json")
        }
        if "large" not in receipts or any(
            r.get("status") != "submitted" for r in receipts.values()
        ):
            return
        ids = {
            g: r.get("stdout", "").strip().split(";")[0] for g, r in receipts.items()
        }
        if not all(x.isdigit() for x in ids.values()):
            return
        try:
            result = subprocess.run(
                [
                    "squeue",
                    "--noheader",
                    "--user",
                    os.environ["USER"],
                    "--states=all",
                    "--format=%F",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            live = set(result.stdout.split())
            if ids["large"] not in live or not all(x.isdigit() for x in live):
                return
            limit = cfg["max_parallel"] - sum(
                cfg["group_parallel"][g] for g in ids if g != "large" and ids[g] in live
            )
            if limit <= state["large_cap"]:
                return
            subprocess.run(
                [
                    "scontrol",
                    "update",
                    "JobId=" + ids["large"],
                    "ArrayTaskThrottle=" + str(limit),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            state = {"large_cap": limit}
        except (OSError, subprocess.SubprocessError) as exc:
            state["error"] = str(exc)
        write_json(record, state)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("receipt")
    p.add_argument("job")
    p.add_argument("--failure")
    p.add_argument("--claim", action="store_true")
    a = p.parse_args()
    if a.claim:
        if a.failure:
            p.error("Cannot combine --claim and --failure")
        print(claim(a.receipt, a.job))
        raise SystemExit(0)
    raise SystemExit(75 if update(a.receipt, a.job, a.failure) else 0)
