"""`faqengine companion ...`: run the local companion, see what this computer can run, and manage pairings.

    faqengine companion run [--port 47890] [--origin https://app.example.com]   start it; it prints a pairing code (press Enter for a new one)
    faqengine companion recommend                                               what this computer is and which local models fit it (no network, no runtime needed)
    faqengine companion status                                                  the runtimes found on this computer and their models
    faqengine companion pairings [--remove KEY_ID]                              the browsers paired with this companion

Nothing here binds to any address but 127.0.0.1. Platforms: macOS arm64 is the one it was run on; Linux and Windows are untested on real machines (docs/local-companion.md)."""
from __future__ import annotations

import json
import sys
import threading

from . import hardware, models
from .auth import Pairings
from .runtimes import LlamaCpp, Ollama
from .server import Companion, serve


LABEL = "ai.knovent.companion"
ORIGIN_OK = __import__("re").compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")


def launch_agent_plist(executable: str, origins: list[str], port: int = 47890, log: str = "~/Library/Logs/knovent-companion.log") -> str:
    """The macOS LaunchAgent that starts the companion when the person logs in and keeps it running. It trusts only the website addresses it is given (a browser sets Origin itself and a page cannot
    forge it), so those pages pair with it without a code; any other page still needs the code. It binds 127.0.0.1 only, like the companion always does."""
    from xml.sax.saxutils import escape

    args = [executable, "companion", "run", "--port", str(port)] + [x for o in origins for x in ("--origin", o)]
    items = "".join(f"\n    <string>{escape(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{items}
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StandardOutPath</key><string>{escape(log)}</string>
  <key>StandardErrorPath</key><string>{escape(log)}</string>
</dict>
</plist>
"""


def _install_service(origins: list[str], port: int) -> int:
    import subprocess
    from pathlib import Path

    if sys.platform != "darwin":
        print("The background service is built for macOS only. On other systems, run `faqengine companion run --origin <your site address>` when you need it.")
        return 1
    bad = [o for o in origins if not ORIGIN_OK.match(o.rstrip("/"))]
    if not origins or bad:
        print("Give the address of your website with --origin, for example:  --origin https://your-site.vercel.app" + (f"   (not valid: {', '.join(bad)})" if bad else ""))
        return 1
    exe = str(Path(sys.argv[0]).resolve()) if Path(sys.argv[0]).name == "faqengine" else str(Path(sys.executable).parent / "faqengine")
    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(launch_agent_plist(exe, [o.rstrip("/") for o in origins], port, str(Path.home() / "Library" / "Logs" / "knovent-companion.log")))
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)  # replaces an earlier install
    subprocess.run(["pkill", "-f", "faqengine companion run"], capture_output=True)  # a copy started by hand would hold the port
    done = subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True, text=True)
    if done.returncode:
        print("Could not start the service: " + (done.stderr or done.stdout).strip()[:200])
        return 1
    print(f"Installed. The companion now starts when you log in and trusts: {', '.join(origins)}.\nNothing else changes: it listens on 127.0.0.1 only. Remove it any time with:  faqengine companion uninstall")
    return 0


def _uninstall_service() -> int:
    import subprocess
    from pathlib import Path

    plist = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    existed = plist.exists()
    plist.unlink(missing_ok=True)
    print("Removed the background companion." if existed else "The background companion was not installed.")
    return 0


def _start_ollama_if_installed() -> None:
    """Ollama (the program that runs the model) is installed by the person; if it is there but not running, start it so nobody has to. Nothing is installed or downloaded."""
    import shutil
    import subprocess
    import time
    import urllib.request

    def up() -> bool:
        try:
            urllib.request.urlopen("http://127.0.0.1:11434", timeout=1).close()
            return True
        except Exception:  # noqa: BLE001 (any failure means "not running")
            return False

    if up() or not shutil.which("ollama"):
        return
    try:
        subprocess.Popen(["ollama", "serve"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return
    for _ in range(10):
        time.sleep(0.5)
        if up():
            print("Started Ollama for you.")
            return


def build(sub) -> None:
    p = sub.add_parser("companion", help="the local companion: lets the hosted application use a model on this computer, with the evidence staying here")
    cs = p.add_subparsers(dest="companion_cmd", required=True)
    r = cs.add_parser("run", help="start the companion on 127.0.0.1")
    r.add_argument("--port", type=int, default=47890)
    r.add_argument("--origin", action="append", default=[], help="an origin allowed to ask for a pairing preflight (the hosted application's address); this origin pairs automatically, without a code (the application served on this computer always does); any other origin needs the code")
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
    origins = set(args.origin) | {o.strip().rstrip("/") for o in os.environ.get("FAQENGINE_APP_ORIGIN", "").split(",") if o.strip()}
    try:
        httpd = serve(args.port, comp, origins)
    except OSError:
        print(f"Port {args.port} is already in use: a companion is probably already running on this computer (in another window).\nStop it first with:  pkill -f 'companion run'   then start this again.")
        return 1
    _start_ollama_if_installed()
    print(f"Pairing code: {comp.pairings.new_code()}   (valid 5 minutes, single use; enter it in the application's Settings; press Enter here for a new one)")

    def more():
        for _ in sys.stdin:
            print(f"Pairing code: {comp.pairings.new_code()}")

    threading.Thread(target=more, daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0
