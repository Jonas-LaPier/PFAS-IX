import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path


class InterruptedCalculation(RuntimeError):
    pass


def process_states(pgid):
    result = subprocess.run(
        ["ps", "-eo", "pgid,stat"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not lines or lines[0] != ["PGID", "STAT"]:
        raise RuntimeError("Unrecognized process list; quiescence cannot be verified")
    if any(len(fields) != 2 or not fields[0].isdigit() for fields in lines[1:]):
        raise RuntimeError("Incomplete process list; quiescence cannot be verified")
    return [
        fields[1]
        for fields in lines[1:]
        if int(fields[0]) == pgid and not fields[1].startswith("Z")
    ]


class Supervisor:
    def __init__(self, interval=43200, poll=2):
        self.interval = interval
        self.poll = poll
        self.child = None
        self.interruption = None
        self.next_snapshot = time.monotonic() + interval
        self.handlers = {}

    def install(self):
        for name in ("SIGUSR1", "SIGTERM", "SIGINT"):
            signum = getattr(signal, name)
            self.handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self.request_stop)

    def request_stop(self, signum, frame):
        self.interruption = signum

    def restore(self):
        for signum, handler in self.handlers.items():
            signal.signal(signum, handler)

    def send(self, signum):
        if self.child:
            try:
                os.killpg(self.child.pid, signum)
            except ProcessLookupError:
                pass

    def stop(self):
        if not self.child:
            return
        self.send(signal.SIGTERM)
        self.send(signal.SIGCONT)
        deadline = time.monotonic() + 20
        while process_states(self.child.pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if process_states(self.child.pid):
            self.send(signal.SIGKILL)
        self.child.wait(timeout=20)
        deadline = time.monotonic() + 20
        while process_states(self.child.pid) and time.monotonic() < deadline:
            time.sleep(0.2)
        if process_states(self.child.pid):
            raise RuntimeError("Gaussian descendants remain active; backup is unsafe")
        self.child = None

    def consistent_snapshot(self, callback):
        self.send(signal.SIGSTOP)
        try:
            deadline = time.monotonic() + 20
            while True:
                states = process_states(self.child.pid)
                if all(s.startswith("T") for s in states):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Gaussian did not pause; backup is unsafe")
                time.sleep(0.2)
            callback("progress; checkpoint unvalidated")
        finally:
            self.send(signal.SIGCONT)
        self.next_snapshot = time.monotonic() + self.interval

    def run(self, command, inp, out, cwd, env, snapshot, observe):
        if self.interruption:
            raise InterruptedCalculation("Allocation warning received before stage")
        self.child = subprocess.Popen(
            command,
            stdin=inp,
            stdout=out,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        try:
            while self.child.poll() is None:
                if self.interruption:
                    self.stop()
                    raise InterruptedCalculation(
                        "Allocation interrupted by signal " + str(self.interruption)
                    )
                observe()
                if time.monotonic() >= self.next_snapshot:
                    self.consistent_snapshot(snapshot)
                time.sleep(self.poll)
            rc = self.child.returncode
            if process_states(self.child.pid):
                self.stop()
                raise RuntimeError("Gaussian exited with live descendants")
            self.child = None
            return rc
        except BaseException:
            self.stop()
            raise


def telemetry(scratch):
    data = {
        "hostname": socket.gethostname(),
        "scratch": str(scratch),
        "scratch_free_bytes": shutil.disk_usage(scratch).free,
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    for path in ("/proc/meminfo", "/proc/self/status"):
        if Path(path).exists():
            data[Path(path).name] = Path(path).read_text()
    if Path("/proc/self/cgroup").exists():
        data["cgroup"] = Path("/proc/self/cgroup").read_text()
        readings = {}
        for line in data["cgroup"].splitlines():
            _, controllers, relative = line.split(":", 2)
            base = (
                Path("/sys/fs/cgroup")
                / ("memory" if "memory" in controllers.split(",") else "")
                / relative.lstrip("/")
            )
            for name in (
                "memory.current",
                "memory.peak",
                "memory.events",
                "memory.max",
                "memory.max_usage_in_bytes",
                "memory.failcnt",
                "memory.limit_in_bytes",
            ):
                p = base / name
                if p.is_file():
                    readings[str(p)] = p.read_text().strip()
        data["memory_accounting"] = readings
    return data
