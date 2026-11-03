"""`faqengine companion ...`: run the local companion, see what this computer can run, and manage pairings.

    faqengine companion run [--port 47890] [--origin https://app.example.com]   start it; it prints a pairing code (press Enter for a new one)
    faqengine companion recommend                                               what this computer is and which local models fit it (no network, no runtime needed)
    faqengine companion status                                                  the runtimes found on this computer and their models
    faqengine companion pairings [--remove KEY_ID]                              the browsers paired with this companion

Nothing here binds to any address but 127.0.0.1. Platforms: macOS arm64 is the one it was run on; Linux and Windows are untested on real machines (docs/local-companion.md)."""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

from . import hardware, models
from .auth import Pairings
from .runtimes import LlamaCpp, Ollama
from .server import Companion, serve


LABEL = "ai.knovent.companion"


def launch_agent_plist(program_args: list[str], origins: list[str], port: int = 47890, log: str = "~/Library/Logs/knovent-companion.log", label: str = LABEL) -> str:
    """The macOS LaunchAgent that starts the companion when the person logs in and keeps it running. `program_args` is the program to run (the installed companion, or `python -m ...` when run from
    source); `run`, the port and the origins are added here. It trusts only the website addresses it is given (a browser sets Origin itself and a page cannot forge it), so those pages pair
    with it without a code; any other page still needs the code. It binds 127.0.0.1 only, like the companion always does."""
    from xml.sax.saxutils import escape

    args = [*program_args, "run", "--port", str(port), "--replace"] + [x for o in origins for x in ("--origin", o)]
    items = "".join(f"\n    <string>{escape(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{escape(label)}</string>
  <key>ProgramArguments</key><array>{items}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StandardOutPath</key><string>{escape(log)}</string>
  <key>StandardErrorPath</key><string>{escape(log)}</string>
</dict>
</plist>
"""


def _install_service(origins: list[str], port: int) -> int:
    """The developer's way to install the login service from a source checkout (customers use the installer program, companion/installer.py)."""
    from . import installer

    res = installer.install(origins, port=port, ui=installer.ConsoleUI(yes=True), program_args=[str(Path(sys.argv[0]).resolve()), "companion"] if Path(sys.argv[0]).name == "faqengine" else None)
    print(res.summary())
    return 0 if res.ok else 1


def _uninstall_service() -> int:
    from . import installer

    res = installer.uninstall(ui=installer.ConsoleUI(yes=True))
    print(res.summary())
    return 0 if res.ok else 1


def start_runtime_in_background(comp: Companion) -> None:
    """Starts the model runtime if it is installed and not running, and keeps watching it (companion/lifecycle.py). Returns at once: the companion is usable while this happens."""
    if comp.supervisor is None or os.environ.get("KNOVENT_NO_RUNTIME"):  # (a test run must not start a real runtime)
        return

    def go() -> None:
        comp.supervisor.ensure_running(wait=20.0)
        comp.supervisor.watch()

    threading.Thread(target=go, daemon=True, name="ollama-start").start()


def build(sub) -> None:
    p = sub.add_parser("companion", help="the local companion: lets the hosted application use a model on this computer, with the evidence staying here")
    cs = p.add_subparsers(dest="companion_cmd", required=True)
    r = cs.add_parser("run", help="start the companion on 127.0.0.1")
    r.add_argument("--port", type=int, default=47890)
    r.add_argument("--origin", action="append", default=[], help="an origin allowed to ask for a pairing preflight (the hosted application's address); this origin pairs automatically, without a code (the application served on this computer always does); any other origin needs the code")
    r.add_argument("--replace", action="store_true", help="used by the login service: replace a companion that is already running (a manually started copy, or the version being upgraded)")
    r.add_argument("--allow-custom-models", action="store_true", help="let a paired browser ask Ollama to download models that are not in the companion's list")
    i = cs.add_parser("install", help="start the companion automatically when you log in (macOS), trusting your website's address so it pairs without a code")
    i.add_argument("--origin", action="append", default=[], help="your website's address, e.g. https://your-site.vercel.app (repeat for more than one)")
    i.add_argument("--port", type=int, default=47890)
    cs.add_parser("uninstall", help="stop starting the companion at login and remove the background service")
    cs.add_parser("recommend", help="what this computer can run")
    cs.add_parser("status", help="runtimes and models found on this computer")
    q = cs.add_parser("pairings", help="list or remove paired browsers")
    q.add_argument("--remove")
    p.set_defaults(fn=cmd_companion)


def cmd_companion(args) -> int:
    if args.companion_cmd == "install":
        return _install_service(list(args.origin), args.port)
    if args.companion_cmd == "uninstall":
        return _uninstall_service()
    if args.companion_cmd == "recommend":
        hw = hardware.detect()
        rec = models.recommend(hw)
        h = rec["hardware"]
        print(f"{h['os']} {h['os_version']} · {h['arch']} · {h['cpu']} · {h['cores']} cores · {h['ram_gb']} GB memory")
        for g in h["gpus"]:
            print(f"  GPU: {g['name']} ({g['vendor']}, {g['vram_gb']} GB{', shared with the system' if g['unified'] else ''}{'' if g['usable_for_models'] else ', not used for models'})")
        print("\nCandidate models (estimates from parameter counts, not measurements; not benchmarked on this product's task):")
        for m in rec["models"]:
            print(f"  {m['id']:<16} {m['speed']:<9} {m['verdict']:<12} {m['runs_on']:<4} {m['why']}")
        if rec["start_with"]:
            print(f"\nStart with: {rec['start_with']}.  Other options by speed: " + ", ".join(f"{k}: {v}" for k, v in rec["suggested"].items()))
        for x in rec["limits"]:
            print(f"\nNote: {x}")
        return 0
    if args.companion_cmd == "status":
        print(json.dumps(Companion(Pairings(), {"ollama": Ollama(), "llamacpp": LlamaCpp()}).status(), indent=2))
        return 0
    if args.companion_cmd == "pairings":
        p = Pairings()
        if args.remove:
            print("removed" if p.remove(args.remove) else "no such pairing")
            return 0
        for x in p.listing():
            print(f"{x['key_id']}  {x['origin']}  {x['label']}")
        return 0
    comp = Companion(Pairings(), allow_custom_models=args.allow_custom_models)
    import os

    from .instance import Instance
    from .origins import TrustedOrigins

    try:  # the websites that may pair without a code: the persisted file (written by the installer), plus any a developer names for this run only
        origins = TrustedOrigins(session=[*args.origin, *[o for o in os.environ.get("FAQENGINE_APP_ORIGIN", "").split(",") if o.strip()]])
    except ValueError as e:
        print(f"Not started: {e}")
        return 1
    inst = Instance(port=args.port)
    got, why = inst.acquire(replace=bool(getattr(args, "replace", False)))
    if not got:
        print(f"Not started: {why}.")
        return 0
    try:
        httpd = serve(args.port, comp, origins)
    except OSError:
        inst.release()
        print(f"Port {args.port} is already in use by another program.")
        return 1
    start_runtime_in_background(comp)
    print(f"Pairing code: {comp.pairings.new_code()}   (valid 5 minutes, single use; enter it in the application's Settings; press Enter here for a new one)")

    def more():
        for _ in sys.stdin:
            print(f"Pairing code: {comp.pairings.new_code()}")

    threading.Thread(target=more, daemon=True).start()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))  # the login service stops it with SIGTERM: stop cleanly
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
        inst.release()
        if comp.supervisor:
            comp.supervisor.stop()
    return 0
