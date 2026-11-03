"""THE MODEL RUNTIME'S LIFECYCLE. Ollama is the program that runs the model. The companion looks for it, starts it when it is installed but not running, watches it, and starts it again if it stops, so
nobody has to type `ollama serve`. Rules it keeps:

* one runtime only: it never starts a second copy. If something already answers on Ollama's address (the Ollama app, a copy started by hand) it is used and left alone;
* it stops only what it started itself;
* a runtime that keeps failing is not restarted forever: after `max_restarts` starts inside `window` seconds it stops trying and says so;
* it only starts a program that is already installed. Installing Ollama is a separate step the person approves (installer.py); nothing is downloaded here."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .runtimes import Ollama

KNOWN_BINARIES = (
    "/opt/homebrew/bin/ollama",
    "/usr/local/bin/ollama",
    "/Applications/Ollama.app/Contents/Resources/ollama",
    "~/Applications/Ollama.app/Contents/Resources/ollama",
)


def _adapter():
    try:  # imported here: platforms imports the CLI, which imports the server, which imports this module
        from .platforms import adapter_for

        return adapter_for()
    except NotImplementedError:
        return None


def find_ollama(which=shutil.which, exists=os.path.isfile, extra: tuple[str, ...] = ()) -> str | None:
    """Where Ollama's command is, if it is installed: on the PATH (a login service has a short one), or where this operating system's installer puts it (macOS: Homebrew and the Ollama app; Windows: the
    per-user and the all-users folders; Linux: /usr/local/bin and the user's own folders). The places are not assumed to be the same everywhere."""
    found = which("ollama")
    if found:
        return found
    a = _adapter()
    for p in (*extra, *(a.ollama_candidates() if a else KNOWN_BINARIES)):
        p = os.path.expanduser(p)
        if exists(p) and os.access(p, os.X_OK):
            return p
    return None


def orphaned_runners(ps_output: str | None = None) -> list[int]:
    """Model runner processes Ollama left behind when its server died (their parent is gone: process 1 on macOS and Linux). Each holds the whole model in memory, and a restart loads another copy, so on a small
    computer a few crashes would exhaust the memory. Only processes whose command line names Ollama's runner AND the Ollama models folder are listed. Not implemented for Windows, where the runners end with their server."""
    if ps_output is None:
        if sys.platform == "win32":
            return []
        try:
            ps_output = subprocess.run(["ps", "-Ao", "pid=,ppid=,command="], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return []
    out = []
    for line in ps_output.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[1] == "1" and parts[0].isdigit() and "ollama" in parts[2].lower() and ("llama-server" in parts[2] or " runner" in parts[2]) and ".ollama" in parts[2]:
            out.append(int(parts[0]))
    return out


def reap_orphaned_runners(terminate=lambda pid: os.kill(pid, 15), ps_output: str | None = None) -> list[int]:
    gone = []
    for pid in orphaned_runners(ps_output):
        try:
            terminate(pid)
            gone.append(pid)
        except OSError:
            pass
    return gone


class OllamaSupervisor:
    def __init__(self, runtime: Ollama | None = None, spawn=subprocess.Popen, locate=find_ollama, interval: float = 10.0, max_restarts: int = 5, window: float = 600.0, clock=time.monotonic):
        self.rt = runtime or Ollama()
        self.spawn, self.locate, self.interval, self.max_restarts, self.window, self.clock = spawn, locate, interval, max_restarts, window, clock
        self._child = None
        self._starts: list[float] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # ---- what is true now
    def running(self) -> bool:
        return bool(self.rt.detect().get("running"))

    def state(self) -> dict:
        with self._lock:
            ours = self._child is not None and self._child.poll() is None
            return {"installed": self.locate() is not None, "running": self.running(), "started_by_companion": ours, "restarts": max(0, len(self._starts) - 1), "last_error": self.last_error}

    # ---- starting it
    def ensure_running(self, wait: float = 25.0) -> bool:
        """True when Ollama answers. Starts it if it is installed and nothing answers; never a second copy; waits for it to be ready."""
        with self._lock:
            if self.running():
                self.last_error = None
                return True
            if self._child is not None and self._child.poll() is None:
                pass  # we started it and it is still coming up: wait, do not start another
            else:
                binary = self.locate()
                if binary is None:
                    self.last_error = "not_installed"
                    return False
                reap_orphaned_runners()  # a server that crashed may have left its model in memory: free it before a new one loads another copy
                now = self.clock()
                self._starts = [t for t in self._starts if now - t < self.window]
                if len(self._starts) >= self.max_restarts:
                    self.last_error = "gave_up"
                    return False
                try:
                    a = _adapter()
                    self._child = self.spawn([binary, "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **(a.spawn_kwargs() if a else {"start_new_session": True}))
                except OSError:
                    self.last_error = "could_not_start"
                    return False
                self._starts.append(now)
        deadline = self.clock() + wait
        while self.clock() < deadline and not self._stop.is_set():
            if self.running():
                self.last_error = None
                return True
            time.sleep(0.3)
        self.last_error = self.last_error or "did_not_become_ready"
        return False

    # ---- watching it
    def watch(self) -> None:
        """Checks every `interval` seconds and starts the runtime again if it stopped. Returns at once; the loop runs in the background until `stop`."""
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(self.interval):
                try:
                    if not self.running():
                        self.ensure_running(wait=15.0)
                except Exception:  # noqa: BLE001 (a fault in the watcher must never take the companion down)
                    self.last_error = "watcher_error"

        self._stop.clear()
        self._thread = threading.Thread(target=loop, daemon=True, name="ollama-watch")
        self._thread.start()

    def stop(self) -> None:
        """Stops watching, and stops the runtime only if this supervisor started it."""
        self._stop.set()
        with self._lock:
            child, self._child = self._child, None
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=8)
            except Exception:  # noqa: BLE001
                child.kill()
