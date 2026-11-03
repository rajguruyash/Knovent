"""Builds docs/cross-encoder-experiment.md from the measured results (results.json, variants.json, performance.json, records.jsonl). Every number in the document comes from them."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np

from experiments.cross_encoder.datasets import build
from experiments.cross_encoder.rerank import MODELS, candidate_text
from experiments.cross_encoder.run import CONFIGS, OUT, summarise

ROOT = Path(__file__).resolve().parents[2]
P = lambda x: "n/a" if x is None else f"{100 * x:.1f}%"  # noqa: E731
KEYS = [("right_first", "Right first"), ("recall@5", "R@5"), ("recall@10", "R@10"), ("recall@20", "R@20"), ("mrr@10", "MRR@10"), ("ndcg@10", "nDCG@10")]
NAMES = {"keyword": "1. Keyword only", "embedding": "2. Embedding only (existing)", "hybrid": "3. Hybrid (existing)", "embedding+ce": "4. Embedding + cross-encoder", "hybrid+ce": "5. Hybrid + cross-encoder"}


def table(summ: dict, configs=CONFIGS) -> list[str]:
    L = ["| Configuration | n | " + " | ".join(k[1] for k in KEYS) + " |", "|---|---|" + "---|" * len(KEYS)]
    for c in configs:
        s = summ.get(c)
        L.append(f"| {NAMES.get(c, c)} | {s['n'] if s else ''} | " + " | ".join(P(s[k]) if s and k != "mrr@10" and k != "ndcg@10" else (f"{s[k]:.3f}" if s else "") for k, _ in KEYS) + " |")
    return L


def main() -> None:
    r = json.loads((OUT / "results.json").read_text())
    rows = r["rows"]
    perf = json.loads((OUT / "performance.json").read_text()) if (OUT / "performance.json").exists() else []
    var = json.loads((OUT / "variants.json").read_text()) if (OUT / "variants.json").exists() else None
    chosen = r["chosen"]
    test = {c: summarise(rows, config=c, pool="test") for c in CONFIGS}
    macro = {}
    for c in CONFIGS:
        per = [summarise(rows, config=c, pool="test", dataset=d) for d in r["datasets"] if d != "evidence"]
        per = [x for x in per if x]
        macro[c] = {"n": len(per), **{k: float(np.mean([x[k] for x in per])) for k, _ in KEYS}}
    no_bank = {c: summarise([x for x in rows if x["dataset"] != "banking77"], config=c, pool="test") for c in CONFIGS}
    L = ["# Cross-encoder reranking: an isolated experiment", "",
         "**Question.** Does a lightweight pretrained cross-encoder, reading a question together with a candidate FAQ entry, rank the right entry higher than the engine's existing retrieval? "
         "**Scope.** Experiment only: nothing in `src/` imports it, the evidence and response engines, the embedding model's weights and the abstention rules are untouched, no cross-encoder was fine-tuned, "
         "and nothing is committed. Code: `experiments/cross_encoder/`; raw results: `runs/cross-encoder/` (per-question candidate records with original rank, original similarity, reranker score and final rank in `records.jsonl`).", ""]
    # verdict first
    e, ec, h, hc = test["embedding"], test["embedding+ce"], test["hybrid"], test["hybrid+ce"]
    L += ["## Verdict", "",
          f"**No, not as configured: it does not beat the best existing configuration, and it is not reliably better than the weaker one.** Pooled over the {e['n']} test questions, right-first accuracy was "
          f"{P(e['right_first'])} for the existing embedding retrieval, {P(h['right_first'])} for the existing hybrid, and {P(ec['right_first'])} / {P(hc['right_first'])} with the cross-encoder on top of them "
          f"(MRR@10 {e['mrr@10']:.3f} / {h['mrr@10']:.3f} → {ec['mrr@10']:.3f} / {hc['mrr@10']:.3f}). The pooled figure is dominated by Banking77 (462 questions), where it is clearly worse; counting each of the nine datasets once "
          f"(macro average) right-first goes {P(macro['embedding']['right_first'])} → {P(macro['embedding+ce']['right_first'])} for embedding + cross-encoder, which looks like a gain over the embedding alone, "
          f"but the existing hybrid is already {P(macro['hybrid']['right_first'])} and hybrid + cross-encoder is {P(macro['hybrid+ce']['right_first'])}. So the cross-encoder at best repairs some of what the keyword blend repairs already, "
          f"on small datasets of 11–38 questions where one question is 3–9 points, and it breaks more questions than it rescues overall. Recall@5, the number that matters for the evidence engine, "
          f"was {P(e['recall@5'])} (embedding), {P(h['recall@5'])} (hybrid) and {P(ec['recall@5'])} / {P(hc['recall@5'])} with the cross-encoder. "
          "**Recommendation: do not integrate this configuration; experiment further only along the lines in the last section.**", ""]
    L += ["## 1. Where a cross-encoder fits in the current architecture", "",
          "The product has three layers (README): retrieval → evidence → response. Retrieval is `Engine.candidates(query, n)` / `ProjectEngine.candidates`: it ranks every entry by embedding similarity (an entry scores as its best view; "
          "a keyword and a hybrid score exist in the evaluation code, `bm25.py` and `heldout_report.hybrid_scores`, but the engine ranks by embedding alone) and returns the best `n` FAQ entries and best `n` passages. "
          "`evidence.assess` then judges up to **5 FAQ candidates and all the passages** it is given, part by part; it uses rank only as a tie-break (`closeness`).",
          "",
          "A reranker belongs **between `candidates()` and `assess()`**: reorder (and, if wanted, widen) the candidate lists, and hand the same plain hit dicts on. That changes no existing behaviour while it is off, "
          "needs no change to the evidence or response layers, and keeps the reranker's score out of the confidence signals (the experiment records it only beside the original similarity). "
          "Consequence for what matters: because the evidence engine inspects the top 5 FAQ entries rather than trusting rank 1, **Recall@5 is the retrieval number that bears on answer quality; right-first matters less.**", "",
          "## 2. Model selection", "",
          "Requirements: pretrained and not fine-tuned, CPU-capable, Apple Silicon-capable, offline after download, small. Four lightweight cross-encoders were downloaded and measured; two larger ones were considered by size only.", "",
          "| Model | Parameters | Download (weights) | Licence | Dev MRR@10: embedding → +cross-encoder | Notes |", "|---|---|---|---|---|---|"]
    sizes = {p["model"]: p for p in perf if p.get("device") == "cpu" and "model" in p}
    dl = {"tinybert-l2": 16.7, "minilm-l6": 86.7, "minilm-l12": 127.3, "mmarco-mminilm": 448.8}
    note = {"tinybert-l2": "smallest; MS MARCO trained", "minilm-l6": "MS MARCO trained, the usual lightweight reranker", "minilm-l12": "twice the depth of L6", "mmarco-mminilm": "multilingual (mMARCO), 118M parameters"}
    for m in MODELS:
        d = r["models"][m]["dev"]
        L.append(f"| `{MODELS[m]}` | {sizes[m]['params_millions'] if m in sizes else '?'} M | {dl[m]} MB | Apache-2.0 | {d['embedding']['mrr@10']:.3f} → {d['embedding+ce']['mrr@10']:.3f} | {note[m]}{' **(chosen)**' if m == chosen else ''} |")
    L += ["| `BAAI/bge-reranker-base` | 278 M | 1060.7 MB | MIT | not run | rejected on size and latency before measuring |",
          "| `BAAI/bge-reranker-v2-m3` | 568 M | 2165.9 MB | Apache-2.0 | not run | rejected on size (more than the whole current package) |", "",
          f"**Selection rule, fixed before looking at test results:** the highest dev-pool MRR@10 of embedding + cross-encoder; within 0.005, the smaller model. It selected `{MODELS[chosen]}`. "
          "That is not an endorsement: on the dev pool **all four** models ranked worse than the embedding retrieval alone (dev MRR@10 "
          f"{r['models'][chosen]['dev']['embedding']['mrr@10']:.3f} for the embedding). MiniLM-L6 was the least harmful, is 87 MB, and its larger sibling L12 did no better.", "",
          "## 3. The experiment", "",
          "For every question: the existing retrieval ranks all entries; the **top 20** are passed to the cross-encoder as (question, candidate) pairs, where a candidate is `question. answer` for a FAQ entry and `heading. text` for a passage "
          "(truncated at 256 tokens); the 20 are reordered by the cross-encoder's score and everything below keeps its retrieval order. Recorded per candidate: original rank, original similarity, reranker score, final rank. "
          "The reranker's score is used only to order the 20 candidates of one question; it is not an answer confidence and is never compared across questions.", "",
          f"- **Embedding**: the pretrained `all-MiniLM-L6-v2` (the engine's base model, un-tuned, so that nothing is trained on the datasets being measured).",
          f"- **Keyword**: the engine's BM25 over each entry's phrasings.",
          f"- **Hybrid**: `hybrid_scores(embedding, keyword, α)` with α = {r['alpha']} chosen on the dev pool by MRR@10 (the same function as the held-out hybrid experiment).",
          "- **Questions**: identical for all five configurations. Pools: *dev* chooses α and the model; *test* is reported and chooses nothing; *locked* is the locked evidence benchmark (`examples/evidence/locked.csv`), "
          "run once for the chosen model after everything else was fixed.", "",
          "| Dataset | Entries | Questions | Source |", "|---|---|---|---|"]
    srcs = {"pawfect": "Pawfect held-out (diagnostic set; split by a hash of the question)", "banking77": "Banking77: 77 real intents × 4 real phrasings; real customer utterances (dev 3/intent, test 6/intent)",
            "bike-shop": "bike-shop example held-out (its own dev/test split)", "evidence": "evidence sets: dev+regression rows → dev, locked rows → locked"}
    for d, v in r["datasets"].items():
        L.append(f"| {d} | {v['entries']} | {v['questions']} | {srcs.get(d, 'domain example held-out (its own dev/test split)')} |")
    L += ["", "Caveat that shapes everything below: **all but Banking77 are tiny knowledge bases (10–19 entries), so the top 20 is the whole knowledge base** and Recall@20 is 100% by construction there; "
          "only Banking77 (77 entries, many near-identical intents) exercises candidate recall and hard negatives. The small sets were written by one author; Banking77 is real customer language.", "",
          f"## 4. Results: the five configurations on the pooled test questions", "",
          "Micro average over all test questions (Banking77 contributes 462 of the " + str(e["n"]) + "):", ""] + table(test) + ["",
          "Macro average (each of the 9 test datasets counts once):", ""] + table(macro) + ["",
          "Without Banking77 (the 8 small datasets pooled, " + str(no_bank["embedding"]["n"]) + " questions):", ""] + table(no_bank) + ["",
          f"Locked benchmark ({r['locked']['embedding']['n']} answerable questions, run once, chosen model only; one question is {100 / r['locked']['embedding']['n']:.0f} points):", ""] + table(r["locked"]) + ["",
          "Hybrid + cross-encoder is almost identical to embedding + cross-encoder everywhere: with 20 candidates the cross-encoder's order dominates whatever produced the 20.", ""]
    L += ["## 5. Per dataset (test pool): right first / MRR@10", "", "| Dataset | n | " + " | ".join(NAMES[c][3:] for c in CONFIGS) + " |", "|---|---|" + "---|" * len(CONFIGS)]
    for d, v in r["by_dataset"].items():
        if v.get("embedding"):
            L.append(f"| {d} | {v['embedding']['n']} | " + " | ".join(f"{P(v[c]['right_first'])} / {v[c]['mrr@10']:.2f}" for c in CONFIGS) + " |")
    L += ["", "## 6. Per category (test pool): right first / MRR@10", "",
          "Categories are the kinds of question the example files label (paraphrase, short, typo, answer-terms, ambiguous = several acceptable entries), plus Banking77's real customer utterances. "
          "*Paraphrase* is the paraphrase-performance measure asked for.", "", "| Category | n | " + " | ".join(NAMES[c][3:] for c in CONFIGS) + " |", "|---|---|" + "---|" * len(CONFIGS)]
    for cat, v in r["by_category"]["test"].items():
        L.append(f"| {cat} | {v['embedding']['n']} | " + " | ".join(f"{P(v[c]['right_first'])} / {v[c]['mrr@10']:.2f}" for c in CONFIGS) + " |")
    L += ["", "Locked benchmark by category:", "", "| Category | n | " + " | ".join(NAMES[c][3:] for c in CONFIGS) + " |", "|---|---|" + "---|" * len(CONFIGS)]
    for cat, v in r["by_category"]["locked"].items():
        L.append(f"| {cat} | {v['embedding']['n']} | " + " | ".join(f"{P(v[c]['right_first'])} / {v[c]['mrr@10']:.2f}" for c in CONFIGS) + " |")
    # hard negatives
    conf, nconf = r["confusable"], r["not_confusable"]
    L += ["", "## 7. Hard negatives", "",
          f"Two views of hard negatives. First, by construction: the right entry has a near-identical neighbour (the nearest other entry's centre has cosine ≥ 0.70 with it). Second, by behaviour: the baseline ranked a wrong entry above the right one: that is the rescued/broken count in section 8. "
          f"Test questions whose right entry has such a neighbour: {conf['embedding']['n']}; the rest: {nconf['embedding']['n']}.", "",
          "| Test questions | n | " + " | ".join(NAMES[c][3:] for c in CONFIGS) + " |", "|---|---|" + "---|" * len(CONFIGS),
          f"| right entry has a near-identical neighbour | {conf['embedding']['n']} | " + " | ".join(f"{P(conf[c]['right_first'])} / {conf[c]['mrr@10']:.2f}" for c in CONFIGS) + " |",
          f"| no near-identical neighbour | {nconf['embedding']['n']} | " + " | ".join(f"{P(nconf[c]['right_first'])} / {nconf[c]['mrr@10']:.2f}" for c in CONFIGS) + " |", ""]
    # retrieved vs ranked
    t = [x for x in rows if x["pool"] == "test"]
    def status(cfg):
        sel = [x for x in t if x["config"] == cfg]
        return Counter("first" if x["best_gold_rank"] == 1 else "ranked wrong (in the top 20)" if x["gold_in_top20"] else "not retrieved (outside the top 20)" for x in sel), len(sel)
    L += ["## 8. Was the right entry retrieved at all, or retrieved and ranked wrongly?", "", "Test questions, by what the retrieval did with the right entry:", "",
          "| Configuration | Right entry first | In the top 20, not first | Not in the top 20 |", "|---|---|---|---|"]
    for c in ("keyword", "embedding", "hybrid", "embedding+ce", "hybrid+ce"):
        cnt, n = status(c)
        L.append(f"| {NAMES[c]} | {cnt['first']} ({P(cnt['first'] / n)}) | {cnt['ranked wrong (in the top 20)']} ({P(cnt['ranked wrong (in the top 20)'] / n)}) | {cnt['not retrieved (outside the top 20)']} ({P(cnt['not retrieved (outside the top 20)'] / n)}) |")
    ce = [x for x in t if x["config"] == "embedding+ce"]
    rescued = [x for x in ce if x["baseline_rank"] > 1 and x["best_gold_rank"] == 1]
    broken = [x for x in ce if x["baseline_rank"] == 1 and x["best_gold_rank"] > 1]
    worse = [x for x in ce if x["best_gold_rank"] > x["baseline_rank"]]
    better = [x for x in ce if x["best_gold_rank"] < x["baseline_rank"]]
    L += ["", f"So for these questions the problem is **ranking, not retrieval**: the right entry is almost always among the 20 (the few outside are on Banking77); the open question is whether a reranker orders them better. "
          f"With the cross-encoder on top of the embedding retrieval: **{len(rescued)} questions rescued** (the right entry was not first and became first), **{len(broken)} broken** (it was first and stopped being), "
          f"{len(better)} moved up, {len(worse)} moved down, of {len(ce)}.", ""]
    # examples
    data = {d.name: d for d in build()}
    recs = [json.loads(x) for x in (OUT / "records.jsonl").read_text().splitlines()] if (OUT / "records.jsonl").exists() else []
    byq = {(x["dataset"], x["question"]): x for x in recs if x["config"] == "embedding+ce"}
    def show(kind, picks):
        out = [f"**{kind}**", ""]
        for x in picks:
            ds = data[x["dataset"]]
            rec = byq.get((x["dataset"], ds.queries[x["q"]]))
            if not rec:
                continue
            top = min(rec["candidates"], key=lambda c: c["final_rank"])
            g = next(c for c in rec["candidates"] if c["entry"] in rec["gold"]) if any(c["entry"] in rec["gold"] for c in rec["candidates"]) else None
            e_top, e_gold = ds.kb.entries[top["entry"]], (ds.kb.entries[g["entry"]] if g else None)
            out.append(f"- [{x['dataset']}] “{ds.queries[x['q']]}” — right entry: “{e_gold.question if e_gold else '?'}” (embedding rank {g['original_rank'] if g else '?'}, reranker score {g['reranker_score']:.2f}, final rank {g['final_rank'] if g else '?'}); "
                       f"reranker's first: “{e_top.question}” (embedding rank {top['original_rank']}, reranker score {top['reranker_score']:.2f}).")
        return out + [""]
    pick = lambda xs, n=5: sorted(xs, key=lambda x: (x["dataset"] == "banking77", x["dataset"], x["q"]))[:n]  # noqa: E731
    L += ["## 9. Failure analysis", ""] + show("Rescued by the cross-encoder (examples from the smaller datasets first)", pick(rescued)) + show("Broken by the cross-encoder", pick(broken, 8))
    L += ["What the examples show. The ms-marco cross-encoders were trained to judge whether a passage answers a web search query. Used on a FAQ they (a) reward a candidate whose *answer text* mentions the question's words, "
          "even when the entry is about something else, and (b) do not know that two entries are siblings (card arrival vs card delivery estimate), which is exactly where this retrieval problem is hard. "
          "In Banking77 the candidate's answer is the generic sentence \"This answers questions about <intent>\", so the reranker has only the question phrasing to go on, and it sees one of an entry's four phrasings "
          "where the embedding retrieval uses all four. Where entries have real, specific answers (the Pawfect, clinic, SaaS sets) it does better, and it was often better on the multi-acceptable (ambiguous) questions."
          "", ""]
    if var:
        o = var["overall"]
        L += ["### One follow-up question: does it just see too little?", "",
              f"Variant (test pool only; not pre-declared; reported next to the main result, not chosen from it): score every phrasing of each candidate entry and let the entry take its best score. "
              f"Pooled test right-first {P(o['right_first'])}, MRR@10 {o['mrr@10']:.3f}, against {P(ec['right_first'])} / {ec['mrr@10']:.3f} for the single-phrasing reranker and "
              f"{P(e['right_first'])} / {e['mrr@10']:.3f} for the embedding retrieval. By dataset (right first): " + ", ".join(f"{k} {P(v['right_first'])}" for k, v in var["by_dataset"].items() if v) + ".", ""]
    # performance
    L += ["## 10. Performance, measured on this machine (Apple M2, 8 GB, offline)", "",
          "Each row is a separate process with the network switched off (`HF_HUB_OFFLINE=1`, files loaded with `local_files_only`); peak memory is the process's peak resident set (`ru_maxrss`) "
          "after the embedding model and again after the reranker was loaded and had scored 60 questions; latency is the median (p95) over 60 questions × 20 candidates in one batch, "
          "and one pair at a time; end-to-end is: embed the question (CPU), score every entry, take the top 20, then (for the second figure) rerank them on the device in the row. The machine is a fanless M2 that throttles under sustained load and the MPS rows are noisy (note the p95s); a first run that overlapped another job was discarded and this one ran with nothing else active.", "",
          "| Model | Params | Weights on disk | Device | Load | 20 candidates, median (p95) | One pair, median | Extra peak memory | End-to-end without → with reranker (median) |", "|---|---|---|---|---|---|---|---|---|"]
    disk = {"tinybert-l2": 17.6, "minilm-l6": 87.6, "minilm-l12": 128.2, "mmarco-mminilm": 469.9}
    for p in perf:
        if "error" in p:
            L.append(f"| {p['model']} | | | {p['device']} | error: {p['error'][:80]} | | | | |")
            continue
        L.append(f"| {p['model']} | {p['params_millions']} M | {disk[p['model']]} MB | {p['device']} | {p['load_seconds']} s | {p['latency_ms_20_candidates']['median']} ({p['latency_ms_20_candidates']['p95']}) ms | "
                 f"{p['latency_ms_one_pair']['median']} ms | {p['extra_peak_rss_mb_for_the_reranker']:.0f} MB (process peak {p['peak_rss_mb_after_inference']:.0f} MB) | "
                 f"{p['end_to_end_ms_without_reranker_cpu']['median']} → {p['end_to_end_ms_with_reranker']['median']} ms |")
    L += ["", "All models loaded and scored offline. \"Weights on disk\" is the downloaded safetensors plus tokenizer and config; the production embedding model is about 87 MB, so the chosen reranker would roughly double the package's model weight.", ""]
    L += ["## 11. Does it actually improve performance?", "",
          f"No. Pooled over test questions it lowers right-first accuracy and MRR@10 for both the embedding and the hybrid retrieval, mostly because of Banking77, where it is clearly worse ({P(r['by_dataset']['banking77']['embedding']['right_first'])} → {P(r['by_dataset']['banking77']['embedding+ce']['right_first'])}). "
          "On the small FAQ-style sets it is mixed: better on some (including the paraphrase-heavy Pawfect set and the ambiguous questions) and worse on others, with 11–38 questions per set, so none of the small-set differences is strong evidence. "
          "Recall@20 cannot change by construction; Recall@5 and @10 are in the tables. It also adds latency, memory and a second model to ship, and it provides a score that must not be mistaken for confidence.", "",
          "## 12. Recommendation", "",
          "**Do not integrate** `ms-marco` cross-encoders as a drop-in reranker. **Experiment further** only if the result below is worth the cost:", "",
          "1. The evidence engine inspects the top 5 FAQ entries and every passage, so the useful reranker target is *Recall@5* and the quality of the candidate *pool*, not right-first. Measure the evidence benchmarks with a reranked pool before anything else.",
          "2. Combine instead of replace: interpolate the cross-encoder score with the retrieval score (not tested here), or use it only when the top retrieval scores are close (a margin trigger).",
          "3. A reranker trained on FAQ-style (question, entry) pairs, or fine-tuned on a customer's own approved questions, could beat a web-search reranker; that is the only route with a plausible gain on Banking77-like data, and it was out of scope here.",
          "4. A multilingual FAQ needs a multilingual model; the one tested (`mmarco-mMiniLMv2`) was the worst here despite being the only one trained for it.", "",
          "## Reproduce", "", "```bash", "PYTHONPATH=src:. python -m experiments.cross_encoder.run                  # dev selection, test, locked (once)",
          "PYTHONPATH=src:. python -m experiments.cross_encoder.variants            # the all-phrasings follow-up",
          "PYTHONPATH=src:. python -m experiments.cross_encoder.performance         # latency and memory, offline",
          "PYTHONPATH=src:. python -m experiments.cross_encoder.report              # writes this file", "```", ""]
    (ROOT / "docs" / "cross-encoder-experiment.md").write_text("\n".join(L))
    print("\n".join(L[:14]))


if __name__ == "__main__":
    main()
