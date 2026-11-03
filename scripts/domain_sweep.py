"""Runs the whole pipeline on FAQs from unrelated fields and languages and tabulates what happened, so that "it works on any kind of data" is a
measured statement and not a hope.

    python scripts/domain_sweep.py                      # every folder under examples/domains, writes docs/domain-sweep.md
    python scripts/domain_sweep.py --domains clinic trivia --device cpu
    python scripts/domain_sweep.py --multilingual       # also runs the non-English domains with multilingual-e5-small (downloads ~470 MB once)

A domain folder holds faq.csv and heldout.csv (see scripts/build_domain_examples.py). Each is run twice: with its typed held-out questions
(the honest measurement) and from the FAQ alone (the generated checks), to see that neither path falls apart.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from faqengine import language  # noqa: E402
from faqengine.auto import run_auto  # noqa: E402
from faqengine.encoder import BASES, release_memory  # noqa: E402
from faqengine.kb import load_kb  # noqa: E402


def pct(x) -> str:
    return "n/a" if x is None else f"{100 * x:.0f}%"


def run_domain(folder: Path, base: str, device: str, out: Path) -> dict:
    kb = load_kb(folder / "faq.csv")
    found = language.detect_kb(kb)
    row = {"domain": folder.name, "base": base, "entries": len(kb), "language": found["language"], "error": None}
    t0 = time.time()
    try:
        rep = run_auto(folder / "faq.csv", out / f"{folder.name}-{base}-heldout", device=device, base_key=base, heldout_path=folder / "heldout.csv")
        release_memory()
        h = rep["heldout"]
        ship = h["decision"]["ship"]
        ov = h["test"]["overall"]
        rf = h["refusal"][ship]["test"]
        row.update(n=h["test"]["n"], keyword=ov["bm25"]["hit@1"], base_hit=ov["base"]["hit@1"], shipped=ov[ship]["hit@1"], ship=ship,
                   trained=not rep["training"].get("skipped"), demonstrated=h["decision"]["improvement_demonstrated"], near=rf["off_topic_near_refused"],
                   far=rf["off_topic_far_refused"], wrongly=rf["in_scope_refused"], floor=h["refusal"][ship]["floor"],
                   warned=any("looks like" in v for v in rep["verdict"]), skipped=rep["training"].get("skipped"))
        gen = run_auto(folder / "faq.csv", out / f"{folder.name}-{base}-generated", device=device, base_key=base)
        release_memory()
        sc = gen["selfcheck"]
        row.update(generated=sc["systems"]["tuned"]["hit@1"], probes_left_out=len(sc.get("probes_left_out") or []),
                   generated_refused=sc["off_topic"]["tuned"]["off_topic_rejected"])
    except Exception as e:  # noqa: BLE001 - the point of the sweep is to find what falls apart
        row["error"] = f"{type(e).__name__}: {str(e)[:160]}"
    row["seconds"] = time.time() - t0
    return row


def render(rows: list[dict]) -> str:
    L = ["# Domain sweep: the same pipeline on FAQs from unrelated fields and languages", "",
         "Each folder in `examples/domains/` is a small FAQ with typed held-out questions written in other words (see `scripts/build_domain_examples.py`). "
         "Held-out columns are measured on that domain's test questions only; the dev questions chose the model and the refusal line. "
         "The FAQs and questions were written by one author, not customers, so absolute figures are optimistic: compare the columns, and look for anything that fell apart.", "",
         "| Domain | Text detected as | Model | Entries | Test Qs | Keyword | Base model | Shipped (right first) | Trained? | Near-topic refused | Unrelated refused | Answerable wrongly refused | Generated check | Probes left out | Time |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r["error"]:
            L.append(f"| {r['domain']} | {r['language']} | {r['base']} | {r['entries']} | | | | | | | | | | | **failed: {r['error']}** |")
            continue
        trained = "no: " + r["skipped"] if r["skipped"] else ("yes, kept" if r["ship"] == "tuned" else "yes, base kept")
        L.append(f"| {r['domain']} | {r['language']}{' (warned)' if r['warned'] else ''} | {r['base']} | {r['entries']} | {r['n']} | {pct(r['keyword'])} | {pct(r['base_hit'])} | **{pct(r['shipped'])}** | {trained} | "
                 f"{pct(r['near'])} | {pct(r['far'])} | {pct(r['wrongly'])} | {pct(r['generated'])} ({r['probes_left_out']} probes left out) | {r['probes_left_out']} | {r['seconds']:.0f} s |")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--domains", nargs="*", help="folder names under examples/domains (default: all)")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--multilingual", action="store_true", help="also run non-English domains with multilingual-e5-small")
    ap.add_argument("--out", default=str(ROOT / "runs" / "domains"))
    ap.add_argument("--report", default=str(ROOT / "docs" / "domain-sweep.md"))
    args = ap.parse_args()
    folders = sorted(p for p in (ROOT / "examples" / "domains").iterdir() if p.is_dir() and (not args.domains or p.name in args.domains))
    rows = []
    for folder in folders:
        bases = ["minilm-l6"]
        if args.multilingual and not language.detect_kb(load_kb(folder / "faq.csv"))["english"]:
            bases.append("multilingual-e5-small")
        for base in bases:
            assert base in BASES
            row = run_domain(folder, base, args.device, Path(args.out))
            rows.append(row)
            print(f"{row['domain']:15s} {row['base']:22s} " + (f"FAILED {row['error']}" if row["error"] else f"shipped {pct(row['shipped'])} (keyword {pct(row['keyword'])}, base {pct(row['base_hit'])}) in {row['seconds']:.0f}s"), flush=True)
    Path(args.report).write_text(render(rows))
    print(f"\nwrote {args.report}")


if __name__ == "__main__":
    main()
