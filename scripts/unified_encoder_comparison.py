"""Does fine-tuning on a business's FAQ pairs change how its PASSAGES (documents, spreadsheets, JSON, web pages) are found and answered from?

    python scripts/unified_encoder_comparison.py --tuned runs/pawfect-heldout/model --tuned runs/washo-improved/model [--json out.json]

The shipped encoder is chosen by held-out FAQ questions only (runs/*/report.json: `decision.ship`). Passages are searched with the same encoder, so this measures the one thing that
decision does not see. For the pretrained MiniLM and for each tuned encoder given, the SAME knowledge, the SAME retrieval, evidence and response code and no language model are run on

  * Acme (examples/unified): every kind of source; dev and regression questions (the locked set is never touched here)
  * Pawfect (examples/evidence): a FAQ and a handbook of the same business the tuned encoder was trained on (the case that matters: tuning on a business's FAQ, searching its documents)

and reported side by side, with how far the tuned encoder's vectors are from the pretrained ones. No new model is introduced; the point is whether the existing choice needs a rule for passages.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import unified_benchmark as UB  # noqa: E402
from faqengine.encoder import Encoder  # noqa: E402
from faqengine.kb import KB  # noqa: E402
from faqengine.runtime import answer, evidence  # noqa: E402
from faqengine.runtime.retrieval import HybridIndex  # noqa: E402

EVID = ROOT / "examples" / "evidence"


class Torch:
    """The embedder interface of the benchmarks, over a torch encoder."""

    def __init__(self, enc):
        import torch

        self.enc, self.cpu, self.cache = enc.to(torch.device("cpu")).eval(), torch.device("cpu"), {}

    def embed(self, texts, kind):
        return self.enc.encode(list(texts), kind, device=self.cpu)

    def embed_docs_cached(self, texts):
        miss = [t for t in dict.fromkeys(texts) if t not in self.cache]
        if miss:
            for t, v in zip(miss, self.embed(miss, "doc")):
                self.cache[t] = v
        return np.stack([self.cache[t] for t in texts])


def pipeline(emb, entries):
    kb = KB(entries)
    vecs = emb.embed([v.text for v in kb.views], "doc")
    index = HybridIndex([UB.entry_dict(e) for e in kb.entries], [{"text": v.text, "entry": v.entry, "kind": v.kind} for v in kb.views], vecs)
    sim = lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []  # noqa: E731

    def search(q, on=None):
        faq, passages, diag = index.search(q, emb.embed([q], "query")[0], **({"on": on} if on is not None else {}))
        diag["embed_ms"] = diag["total_ms"] = 0.0
        return faq, passages, diag

    search.model = index.fact_model
    return lambda q: answer.respond(q, search, sim, None, 0.4, evidence.SEM_MIN), vecs


def acme(emb, name):
    rows = [dict(r) for r in csv.DictReader(open(UB.DATA / f"{name}.csv", encoding="utf-8"))]
    entries, _ = UB.load_knowledge(UB.DATA / "knowledge")
    ask, vecs = pipeline(emb, entries)
    res = [UB.judge(r, ask(r["question"])) for r in rows]
    s = UB.summarise(rows, res)
    return {k: s[k] for k in ("questions", "retrieval_evidence_recall", "retrieval_all_gold_found", "correct_answer_rate", "false_answer_rate", "citation_correct", "abstention_precision",
                              "abstention_recall", "sufficiency_precision", "sufficiency_recall")}, vecs


def pawfect(emb, name):
    spec = importlib.util.spec_from_file_location("evidence_benchmark", ROOT / "scripts" / "evidence_benchmark.py")
    eb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eb)
    from faqengine import sources

    entries = sources.load_sources([EVID / "pawfect_faq.csv", EVID / "pawfect_handbook.md", EVID / "policy_update.md"]).entries
    ask, _ = pipeline(emb, entries)
    rows = [{k: (v or "") for k, v in r.items()} for r in csv.DictReader(open(EVID / f"{name}.csv", encoding="utf-8"))]
    ok = answered = answerable = false = 0
    for r in rows:
        good, given = eb.judge(r, ask(r["question"]))
        if r["expected_outcome"] in ("faq", "grounded"):
            answerable += 1
            ok += good
        if given:
            answered += 1
            false += not good
    return {"questions": len(rows), "correct_answer_rate": round(ok / answerable, 3) if answerable else None, "false_answer_rate": round(false / answered, 3) if answered else None,
            "answered": answered, "answerable": answerable, "correct": ok}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tuned", action="append", default=[], help="a trained run's model folder (repeat for several)")
    ap.add_argument("--json")
    args = ap.parse_args()
    encoders = {"pretrained": Torch(Encoder.load("minilm-l6"))}
    for p in args.tuned:
        encoders[Path(p).parent.name] = Torch(Encoder.load(p))
    out: dict = {}
    base_vecs = None
    for label, emb in encoders.items():
        out[label] = {}
        for name in ("dev", "regression"):
            out[label][f"acme {name}"], vecs = acme(emb, name)
        out[label]["pawfect dev"] = pawfect(emb, "dev")
        out[label]["pawfect regression"] = pawfect(emb, "regression")
        if base_vecs is None:
            base_vecs = vecs
        else:
            out[label]["vector_agreement_with_pretrained"] = round(float((vecs * base_vecs).sum(1).mean()), 4)  # mean cosine of the same text under both encoders
    print(json.dumps(out, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
