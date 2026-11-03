"""ONE COMPANION PER COMPUTER. The companion records itself in a lock file (process id, start time, program). A second one does not fight for the port: it either leaves ("already running") or, when it is the
INSTALLED login service, replaces the first (a manually started copy, or the version being upgraded): it asks that process to stop, waits for it to go, and only then takes over. Only a process whose id is
in OUR lock file, whose command line still names Knovent, is ever asked to stop: nothing else is touched."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .auth import default_config_path

NAMES = ("knovent", "faqengine")


def lock_path(port: int = 47890) -> Path:
    return default_config_path().parent / f"companion-{port}.lock"  # one lock per port: a companion on another port is another companion


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def command_of(pid: int) -> str:
    try:
        if sys.platform == "win32":
            return subprocess.run(["powershell", "-NoProfile", "-Command", f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"], capture_output=True, text=True).stdout.strip()
        return subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def stop_process(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
    else:
        os.kill(pid, signal.SIGTERM)


class Instance:
    def __init__(self, path: Path | None = None, port: int = 47890, alive=pid_alive, command=command_of, stop=stop_process, pid: int | None = None):
        self.path, self.alive, self.command, self.stop = path or lock_path(port), alive, command, stop
        self.pid = pid or os.getpid()

    def holder(self) -> dict | None:
        """The live Knovent companion recorded in the lock file, or None (no file, a dead process, or a process id since reused by something else)."""
        try:
            d = json.loads(self.path.read_text())
            pid = int(d["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if pid == self.pid or not self.alive(pid) or not any(n in self.command(pid).lower() for n in NAMES):
            return None
        return d

    def acquire(self, replace: bool = False, wait: float = 15.0, program: str = "") -> tuple[bool, str]:
        """(True, '') when this process now holds the lock. (False, why) when another companion runs and was not replaced."""
        other = self.holder()
        if other is not None:
            if not replace:
                return False, f"a Knovent companion is already running on this computer (process {other['pid']})"
            self.stop(int(other["pid"]))
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and self.alive(int(other["pid"])):
                time.sleep(0.2)
            if self.alive(int(other["pid"])):
                return False, f"the companion that is running (process {other['pid']}) did not stop"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": self.pid, "started": time.time(), "program": program or sys.argv[0]}))
        os.replace(tmp, self.path)
        return True, ""

    def release(self) -> None:
        try:
            d = json.loads(self.path.read_text())
            if int(d.get("pid", -1)) == self.pid:
                self.path.unlink()
        except (OSError, ValueError, TypeError):
            pass
