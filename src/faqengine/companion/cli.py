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
    cs.add_parser("recommend", help="what this computer can run")
    cs.add_parser("status", help="runtimes and models found on this computer")
    q = cs.add_parser("pairings", help="list or remove paired browsers")
    q.add_argument("--remove")
    p.set_defaults(fn=cmd_companion)


def cmd_companion(args) -> int:
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
