"""One training or export job in its own process:  python -m faqengine.studio.worker train|export <project folder>

The website's server starts this and reads status.json and events.jsonl; nothing else is shared, so a crash here cannot take the
page down and the memory training used (a lot, on a small laptop) is returned when the process ends.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

from .store import Store, read_json, write_json


def friendly(e: BaseException) -> str:
    text = f"{type(e).__name__}: {e}"
    low = text.lower()
    if isinstance(e, OSError) and any(w in low for w in ("connect", "offline", "resolve", "max retries", "huggingface", "cache")):
        return ("Could not download the base model (about 90 MB, needed once). Connect to the internet, then press Train again. "
                "After the first download it works offline.")
    if isinstance(e, MemoryError) or "out of memory" in low:
        return "The machine ran out of memory. Close other applications and try again."
    return text[:400]


def set_status(d: Path, **kw) -> None:
    write_json(d / "status.json", {**read_json(d / "status.json", {}), **kw})


def train(d: Path, use_feedback: bool) -> None:
    from ..auto import run_auto
    from . import feedback as fb

    meta = read_json(d / "meta.json")
    store = Store(d.parents[1])
    paths = store.source_paths(meta["id"])
    manual = meta.get("manual") or {}
    signature = store.signature(meta["id"])
    new = d / "run.new"
    shutil.rmtree(new, ignore_errors=True)
    events = open(d / "events.jsonl", "a")

    def on_event(e: dict) -> None:
        events.write(json.dumps(e) + "\n")
        events.flush()
        if e.get("event") == "stage" and e["stage"] in ("reading", "loading_model", "starting", "checking", "saving", "finished"):
            set_status(d, stage=e["stage"])

    try:
        extra = None
        if use_feedback:
            history = store.history(meta["id"])  # every judged test helps, whichever model it was run on
            extra = fb.extra_phrasings(history, {e.id for e in store.kb(meta["id"]).entries}) or None
        run_auto(paths, new, on_event, base_key=meta.get("base") or "minilm-l6", finetune=meta.get("finetune", True), extra=extra, heldout_path=store.heldout_path(meta["id"]),
                 floor=manual.get("floor"), direct=manual.get("direct"))
        shutil.rmtree(d / "run", ignore_errors=True)
        new.rename(d / "run")
        write_json(d / "meta.json", {**read_json(d / "meta.json", meta), "gen": meta.get("gen", 0) + 1, "trained_signature": signature})  # judgements made on an older model prove nothing about this one
        shutil.rmtree(d / "package", ignore_errors=True)
        write_json(d / "export.json", {"state": "idle"})
        set_status(d, state="trained", stage="finished", finished=time.time(), error=None)
    except BaseException as e:  # noqa: BLE001 - whatever went wrong, the page must be told
        traceback.print_exc()
        set_status(d, state="failed", error=friendly(e), finished=time.time())
        shutil.rmtree(new, ignore_errors=True)
    finally:
        events.close()


def export(d: Path) -> None:
    from ..auto import export_run

    meta = read_json(d / "meta.json")
    name = re.sub(r"[^a-z0-9]+", "-", meta["name"].lower()).strip("-")[:40] or "faq-engine"
    try:
        shutil.rmtree(d / "package", ignore_errors=True)
        store = Store(d.parents[1])
        info = export_run(d / "run", d / "package" / name, name, read_json(d / "run" / "thresholds.json"),
                          sources=store.source_paths(meta["id"]), heldout=store.heldout_path(meta["id"]), regression=store.regression_path(meta["id"]),
                          web=[{"file": Path(r["file"]).name, "url": r["url"]} for r in store.sources(meta["id"]) if r.get("url")])
        write_json(d / "export.json", {"state": "done", "zip": Path(info["zip"]).name, "zip_mb": info["zip_mb"], "package_mb": info["package_mb"],
                                       "int8": info["int8_chosen"], "finished": time.time()})
    except BaseException as e:  # noqa: BLE001
        traceback.print_exc()
        write_json(d / "export.json", {"state": "failed", "error": friendly(e)})


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("train", "export"):
        print(__doc__)
        return 2
    d = Path(argv[2])
    (train(d, "--use-feedback" in argv) if argv[1] == "train" else export(d))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
