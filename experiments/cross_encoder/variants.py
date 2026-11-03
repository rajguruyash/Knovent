"""One more question, asked after the main result and answered on the TEST pool only (never the locked pool): the main experiment shows the reranker ONE phrasing and the answer
of each candidate, while the embedding retrieval looks at every phrasing of an entry. Is part of the reranker's deficit just that it saw less?

Variant `embedding+ce(all phrasings)`: score (question, phrasing + answer) for every phrasing of each of the top-20 entries and let the entry take its best score.
This was not chosen by looking at results; it is reported next to the main numbers, and it is not the pre-declared configuration.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch

from experiments.cross_encoder.datasets import build
from experiments.cross_encoder.rerank import MODELS, TOP, CrossEncoder, metrics_for, order, rerank
from experiments.cross_encoder.run import OUT, prepare, summarise
from faqengine.encoder import Encoder, pick_device


def main() -> None:
    res = json.loads((OUT / "results.json").read_text())
    device = pick_device("auto")
    base = Encoder.load("minilm-l6").to(device).eval()
    ce = CrossEncoder(MODELS[res["chosen"]], res["device"])
    rows = []
    for ds in build():
        p = prepare(ds, base, device)
        for i, q in enumerate(ds.queries):
            if ds.pool[i] != "test":
                continue
            s = p["dense"][i]
            cand = order(s)[:TOP]
            pairs, owner = [], []
            for e in cand:
                ent = ds.kb.entries[int(e)]
                for phr in ent.questions[:6]:
                    pairs.append(f"{phr}. {ent.answer}" if ent.answer else phr)
                    owner.append(int(e))
            scores = ce.score(q, pairs)
            best = np.array([max(scores[j] for j, o in enumerate(owner) if o == int(e)) for e in cand])
            m = metrics_for(rerank(s, best)["final"], ds.gold[i])
            rows.append({"dataset": ds.name, "config": "embedding+ce(all phrasings)", "pool": "test", **m})
    out = {"overall": summarise(rows, config="embedding+ce(all phrasings)"),
           "by_dataset": {n: summarise(rows, config="embedding+ce(all phrasings)", dataset=n) for n in sorted({r["dataset"] for r in rows})}}
    (OUT / "variants.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out["overall"]), {k: round(v["right_first"], 2) for k, v in out["by_dataset"].items()})


if __name__ == "__main__":
    main()
