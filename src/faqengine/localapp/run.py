"""`faqengine app`: starts everything the product needs on this computer, in one process tree, and opens the browser: the real engine, the website, the local-model companion (which pairs with
the website by itself) and Ollama if it is installed. Nothing is installed or downloaded here; the website asks before any model is downloaded."""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _up(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=1).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _stop_stale_site(port: int) -> None:
    """Stops the leftover website server on `port`, only if it is this product's (its command line names preview.mjs)."""
    try:
        pids = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=5).stdout.split()
        for pid in pids:
            cmd = subprocess.run(["ps", "-o", "command=", "-p", pid], capture_output=True, text=True, timeout=5).stdout
            if "preview.mjs" in cmd:
                os.kill(int(pid), 15)
                print("Stopped a leftover website server from an earlier run.")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass


def cmd_app(args) -> int:
    from ..companion import cli as ccli
    from ..companion.auth import Pairings
    from ..companion.server import Companion
    from ..companion.server import serve as companion_serve
    from .engine import LocalEngine
    from .server import serve as engine_serve

    node = shutil.which("node")
    if not node:
        print("Node.js is needed for the website (https://nodejs.org). Install it and run this again.")
        return 1
    if not (ROOT / "web" / "public" / "vendor" / "supabase.js").exists():  # the browser library is built, not stored in the repository: a fresh clone builds it once
        npm = shutil.which("npm")
        if not npm:
            print("npm is needed once to build the website (it comes with Node.js: https://nodejs.org).")
            return 1
        print("First run: building the website (a minute, needs internet once)…")
        if subprocess.run([npm, "install", "--silent"], cwd=ROOT / "web").returncode or subprocess.run([npm, "run", "build", "--silent"], cwd=ROOT / "web").returncode:
            print("The website could not be built. Run `npm install && npm run build` inside the web folder to see why.")
            return 1
    site = f"http://127.0.0.1:{args.port}"
    if _up(site):
        if _listening(args.engine_port):
            print(f"Knovent is already running at {site}.")
            if not args.no_browser:
                webbrowser.open(site)
            return 0
        _stop_stale_site(args.port)  # a website server left over from a run whose engine and companion have stopped: it would answer pages with nothing behind it
        for _ in range(20):
            if not _up(site):
                break
            time.sleep(0.25)
    data = Path(args.data).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    print("Starting Knovent…")
    engine = LocalEngine(data)
    secret = secrets.token_hex(24)
    engine_httpd = engine_serve(engine, secret, args.engine_port)
    threading.Thread(target=engine_httpd.serve_forever, daemon=True).start()
    ccli._start_ollama_if_installed()
    try:
        comp = Companion(Pairings(), allow_custom_models=False)
        chttpd = companion_serve(47890, comp, set(), announce=lambda *_: None)
        threading.Thread(target=chttpd.serve_forever, daemon=True).start()
    except OSError:
        print("(a companion is already running on this computer; using it)")
    env = {**os.environ, "KNOVENT_ENGINE": f"http://127.0.0.1:{args.engine_port}", "KNOVENT_SECRET": secret, "KNOVENT_DATA": str(data)}
    proc = subprocess.Popen([node, str(ROOT / "web" / "scripts" / "preview.mjs"), str(args.port)], cwd=ROOT / "web", env=env)
    for _ in range(40):
        if _up(site):
            break
        time.sleep(0.25)
    print(f"\nKnovent is ready: {site}\nYour files and answers stay on this computer. Press Ctrl+C here to stop everything.\n")
    if not args.no_browser:
        webbrowser.open(site)
    import signal

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))  # `pkill` stops the website server too, instead of leaving it behind
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        engine_httpd.shutdown()
    return 0


def build(sub) -> None:
    p = sub.add_parser("app", help="start Knovent on this computer: website, real engine, local model companion (one command)")
    p.add_argument("--port", type=int, default=8768)
    p.add_argument("--engine-port", type=int, default=8769)
    p.add_argument("--data", default="~/.knovent", help="where businesses' files and settings are kept")
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_app)


__all__ = ["build", "cmd_app", "sys"]
