"""Keeps a deployed FAQ engine improving. Run it from anywhere; --package says which package (default: this one).

    python ci/improve.py status                              # what has been learned, which versions exist
    python ci/improve.py add returns "can I post it back"    # teach it one more way of asking an entry (same as POST /feedback)
    python ci/improve.py forget returns "can I post it back" # take a mistaken one back out (live at once; out of the model at the next retraining)
    python ci/improve.py retrain [--refresh-web]             # rebuild from source/ plus everything learned (and web pages read again); promote only if not worse
    python ci/improve.py promote staged-20260101-120000      # make a staged candidate live (when there were no held-out questions to judge it by)
    python ci/improve.py rollback                            # go back to the previous version

`status`, `add`, `promote` and `rollback` need only what the package already has. `retrain` needs the faqengine tool (the one that made
this package) installed: pip install <path or URL of faq-engine>.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "runtime"))

import versions  # noqa: E402  (the package's own runtime/versions.py)


def count(path: Path) -> int:
    return sum(1 for x in path.read_text(encoding="utf-8").splitlines() if x.strip()) if path.is_file() else 0


def cmd_status(pkg: Path, args) -> int:
    data = pkg / "data"
    print(f"package        {pkg}")
    print(f"learned        {count(data / 'learned.jsonl')} questions in data/learned.jsonl (searched live; each retraining trains on all of them)")
    print(f"feedback log   {count(data / 'feedback.jsonl')} entries   asked log {count(data / 'asked.jsonl')} entries")
    print(f"held-out set   {'source/heldout.csv (a candidate is judged on it)' if (pkg / 'source' / 'heldout.csv').is_file() else 'none: a retrained candidate will be staged, not promoted'}")
    old = versions.saved(pkg)
    print(f"saved versions {', '.join(d.name for d in old) if old else 'none'}")
    staged = sorted(d.name for d in (pkg / "versions").glob("staged-*")) if (pkg / "versions").is_dir() else []
    if staged:
        print(f"staged         {', '.join(staged)}")
    return 0


def cmd_add(pkg: Path, args) -> int:
    from engine import Engine

    result = Engine(pkg).learn(args.entry_id, args.question, by="cli")
    print(json.dumps(result))
    return 0


def cmd_forget(pkg: Path, args) -> int:
    path = pkg / "data" / "learned.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    gone = lambda x: (lambda r: r.get("entry_id") == args.entry_id and " ".join(str(r.get("question")).split()).lower() == " ".join(args.question.split()).lower())(json.loads(x))  # noqa: E731
    keep = [x for x in lines if x.strip() and not gone(x)]
    path.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
    print(f"forgot {len([x for x in lines if x.strip()]) - len(keep)} question(s); a running server drops it on its next request, and the model forgets it at the next retraining")
    return 0


def cmd_retrain(pkg: Path, args) -> int:
    try:
        from faqengine.improve import improve_package
    except ImportError:
        sys.exit("retrain needs the faqengine tool installed (the one that made this package): pip install <path or URL of faq-engine>")
    report = improve_package(pkg, promote=not args.no_promote, device=args.device, extra_file=args.learned, refresh_pages=args.refresh_web)
    print(json.dumps(report, indent=1))
    print(f"\n{report['outcome'].upper()}: {report['reason']}")
    return 0 if report["outcome"] != "kept_current" else 3  # a non-zero exit lets a pipeline flag "nothing was deployed"


def cmd_promote(pkg: Path, args) -> int:
    source = pkg / "versions" / args.version
    versions.promote(pkg, source)
    shutil.rmtree(source, ignore_errors=True)
    print(f"{args.version} is live; the version it replaced is in versions/")
    return 0


def cmd_rollback(pkg: Path, args) -> int:
    print(f"went back to {versions.rollback(pkg)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", default=str(PACKAGE))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("add")
    p.add_argument("entry_id")
    p.add_argument("question")
    p.set_defaults(fn=cmd_add)
    p = sub.add_parser("forget")
    p.add_argument("entry_id")
    p.add_argument("question")
    p.set_defaults(fn=cmd_forget)
    p = sub.add_parser("retrain")
    p.add_argument("--no-promote", action="store_true", help="build and test the candidate, but leave it staged")
    p.add_argument("--device", default="auto", help="auto, cpu, mps or cuda")
    p.add_argument("--learned", help="a learned.jsonl downloaded from the server (GET /learned) to include")
    p.add_argument("--refresh-web", action="store_true", help="read the web pages in source/web.json again first (a page that cannot be read keeps its old copy)")
    p.set_defaults(fn=cmd_retrain)
    p = sub.add_parser("promote")
    p.add_argument("version", help="a folder name under versions/, for example staged-20260101-120000")
    p.set_defaults(fn=cmd_promote)
    sub.add_parser("rollback").set_defaults(fn=cmd_rollback)
    args = ap.parse_args()
    return args.fn(Path(args.package).resolve(), args)


if __name__ == "__main__":
    raise SystemExit(main())
