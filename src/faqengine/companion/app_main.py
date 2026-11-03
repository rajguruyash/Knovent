"""The companion as one program (built with PyInstaller into 'Knovent Companion.app'). Double-clicked, it opens the setup. As the login service it runs the companion. Nothing else is reachable
from here: no shell, no file browser, only these commands."""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from . import cli, installer
from .uiselect import select_ui


def _version() -> str:
    from faqengine import __version__

    return __version__


def _build_config() -> dict:
    """The website addresses this build trusts and which channel it is for, written in at build time (companion/build_config.json). A production build holds production addresses only."""
    import importlib.resources as res

    try:
        d = json.loads((res.files(__package__) / "build_config.json").read_text())
        return {"channel": "development" if d.get("channel") == "development" else "production", "origins": [o for o in d.get("origins", []) if isinstance(o, str)]}
    except (OSError, ValueError, FileNotFoundError):
        return {"channel": "production", "origins": []}


def _site_origins() -> list[str]:
    return _build_config()["origins"]


def build_parser() -> argparse.ArgumentParser:
    """The program's command line. The login services of every platform start it with exactly these arguments (companion/platforms.py), which a test checks."""
    ap = argparse.ArgumentParser(prog="knovent-companion")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--port", type=int, default=47890)
    r.add_argument("--origin", action="append", default=[])
    r.add_argument("--replace", action="store_true")
    s = sub.add_parser("setup")
    s.add_argument("--yes", action="store_true", help="approve every question (for scripted tests)")
    s.add_argument("--port", type=int, default=47890)
    s.add_argument("--origin", action="append", default=[])
    s.add_argument("--label", default=installer.LABEL, help="the login service's name (tests use their own, so a real install is never touched)")
    s.add_argument("--app-dir", default=None)
    u = sub.add_parser("uninstall")
    u.add_argument("--label", default=installer.LABEL)
    u.add_argument("--app-dir", default=None)
    sub.add_parser("status")
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("setup", "uninstall", "status") and "--gui" in argv:
        return gui(argv)
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.cmd == "run":
        ns = argparse.Namespace(companion_cmd="run", port=a.port, origin=a.origin, allow_custom_models=False, replace=a.replace)  # only the installed service (started with --replace) takes over from another companion
        return cli.cmd_companion(ns)
    if a.cmd == "setup":
        res = installer.install(a.origin or _site_origins(), channel=_build_config()["channel"], port=a.port, ui=installer.ConsoleUI(yes=a.yes), label=a.label, app_dir=Path(a.app_dir) if a.app_dir else None, version=_version())
        print(res.summary())
        return 0 if res.ok else 1
    if a.cmd == "uninstall":
        res = installer.uninstall(ui=installer.ConsoleUI(yes=True), label=a.label, app_dir=Path(a.app_dir) if a.app_dir else None)
        print(res.summary())
        return 0 if res.ok else 1
    print(json.dumps(installer.status(), indent=2))
    return 0


def gui(argv: list[str]) -> int:
    """What a double-click does: a plain window, no Terminal. Shows where things stand, then sets up, repairs or removes. (Windows lists Knovent under Settings > Apps with `uninstall --gui`.)"""
    ui = select_ui()
    if argv and argv[0] == "uninstall":
        if not hasattr(ui, "confirm") or ui.confirm("Remove Knovent?", "The Knovent companion will stop starting when you log in and will be removed. Ollama and your downloaded models stay.", "Remove", "Cancel"):
            res = installer.uninstall(ui=ui)
            ui.info("Knovent removed" if res.ok else "Could not remove Knovent", res.error or "The companion no longer starts at login. Ollama and your models were left in place.")
        return 0
    st = installer.status()
    if st["service_registered"] and st["answering"]:
        choice = ui.choose("Knovent is set up", "The Knovent companion is running on this Mac.\n\nYou can close this and use the Knovent website.", ["Close", "Repair", "Remove"])
        if choice == "Remove":
            res = installer.uninstall(ui=ui)
            ui.info("Knovent removed" if res.ok else "Could not remove Knovent", res.error or "The companion no longer starts at login. Ollama and your models were left in place.")
        elif choice == "Repair":
            return _setup_gui(ui)
        return 0
    return _setup_gui(ui)


def _setup_gui(ui) -> int:
    origins = _site_origins()
    res = installer.install(origins, ui=ui, version=_version(), channel=_build_config()["channel"])
    if res.ok:
        ui.info("Knovent is ready", "Go back to the Knovent website in your browser. It will find this Mac by itself. If it does not, refresh the page.")
        if origins:
            webbrowser.open(origins[0])
        return 0
    ui.info("Setup did not finish", res.error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
