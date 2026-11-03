"""Benchmark: does fine-tuning on a small FAQ beat an untuned model plus BM25?

Banking77 (customer-support questions, 77 intents, CC BY 4.0) is cast as FAQ retrieval. Each intent becomes
one FAQ entry whose questions are `k` real customer phrasings taken from the train split, standing in for
the phrasings a business writes (or an LLM generates) per entry. The model never sees any other train
question. Dev questions come from the rest of the train split, and every test question comes from the
official test split, so test questions are never seen in training. Test questions from intents that are
NOT in the knowledge base serve as out-of-scope questions.
"""
from __future__ import annotations

import csv
import hashlib
import json
import random
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from . import metrics
from .encoder import Encoder, pick_device
from .evaluate import LABELS, evaluate
from .kb import KB, Entry, Queries, clean
from .paraphrase import Cache
from .train import TrainConfig, train

BASE_URL = "https://raw.githubusercontent.com/PolyAI-LDN/task-specific-datasets/master/banking_data/"


def load_banking77(cache: str | Path = "data") -> dict[str, dict[str, list[str]]]:
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    data: dict[str, dict[str, list[str]]] = {}
    for split in ("train", "test"):
        path = cache / f"banking77_{split}.csv"
        if not path.exists():
            urllib.request.urlretrieve(BASE_URL + f"{split}.csv", path)
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                data.setdefault(row["category"], {"train": [], "test": []})[split].append(clean(row["text"]))
    return data


@dataclass
class Scenario:
    kb: KB
    dev: Queries
    test: Queries


def build_scenario(data, seed: int, n_intents: int, k: int | None, answer_mode: str = "label",
                   dev_per_entry: int = 5, oos_cap: int = 1000, paraphrases: Cache | None = None, use: int | None = None) -> Scenario:
    """k = phrasings per FAQ entry; None means every remaining train question (the 'we have query logs' ceiling).

    With `paraphrases`, an entry keeps its single real phrasing (k must be 1) and gains the first `use` LLM-written
    ones: the same intents, the same first phrasing and the same dev and test questions as the plain k=1 run."""
    if paraphrases is not None and k != 1:
        raise ValueError("LLM paraphrases extend a single real phrasing: use k=1")
    rng = random.Random(seed)
    labels = sorted(data)
    chosen = rng.sample(labels, n_intents)
    others = [lab for lab in labels if lab not in chosen]
    entries, dev_t, dev_g, test_t, test_g, seen = [], [], [], [], [], set()
    for i, label in enumerate(chosen):
        pool = list(dict.fromkeys(data[label]["train"]))
        rng.shuffle(pool)
        dev = pool[:dev_per_entry]
        faq = pool[dev_per_entry:] if k is None else pool[dev_per_entry:dev_per_entry + k]
        topic = label.replace("_", " ").lower()
        answer = f"This answers questions about {topic}." if answer_mode == "label" else ""
        if paraphrases is not None:
            extra = paraphrases.get(faq[0], answer)
            if extra is None:
                raise ValueError(f"no LLM paraphrases cached for {label!r} (seed {seed}); run `faqengine bench-paraphrases` for this seed first")
            faq = faq + extra[:use]
        entries.append(Entry(id=label, questions=faq, answer=answer))
        dev_t += dev
        dev_g += [i] * len(dev)
        seen.update(t.lower() for t in pool)
    for i, label in enumerate(chosen):
        fresh = [t for t in dict.fromkeys(data[label]["test"]) if t.lower() not in seen]
        test_t += fresh
        test_g += [i] * len(fresh)
    oos = [t for lab in others for t in dict.fromkeys(data[lab]["test"]) if t.lower() not in seen]
    rng.shuffle(oos)
    oos = oos[:oos_cap]
    return Scenario(KB(entries), Queries(dev_t, np.array(dev_g)), Queries(test_t + oos, np.array(test_g + [-1] * len(oos))))


def run_scenario(data, seed: int, n_intents: int, k: int | None, cfg: TrainConfig, answer_mode: str, base: Encoder,
                 paraphrases: Cache | None = None, use: int | None = None) -> dict:
    t0 = time.time()
    sc = build_scenario(data, seed, n_intents, k, answer_mode, paraphrases=paraphrases, use=use)
    cfg = TrainConfig(**{**asdict(cfg), "seed": seed})
    tuned, log = train(sc.kb, sc.dev, cfg, base=base)
    rep = evaluate(sc.kb, sc.dev, sc.test, base, tuned, log, device=cfg.device)
    return {
        "base": cfg.base, "n_intents": n_intents, "k": k, "seed": seed, "answer_mode": answer_mode, "config": asdict(cfg),
        "llm_paraphrases": 0 if paraphrases is None else (use or paraphrases.n), "llm_model": None if paraphrases is None else paraphrases.model,
        "systems": rep["systems"], "lift": rep["lift"], "loss": rep["loss"], "thresholds": rep["thresholds"],
        "abstention": rep["abstention"], "training": rep["training"], "meta": rep["meta"], "seconds": time.time() - t0,
    }


def cell_key(base: str, n: int, k, seed: int, answer_mode: str, cfg: TrainConfig, llm: int = 0) -> str:
    knobs = {x: getattr(cfg, x) for x in ("lr", "n_aug", "hard_negatives", "max_steps", "batch_size", "patience")}
    return "|".join([base, str(n), str(k), str(seed), answer_mode, f"llm{llm}", hashlib.md5(json.dumps(knobs, sort_keys=True).encode()).hexdigest()[:6]])


def run_grid(out: str | Path, base: str, ks: list[int | None], seeds: list[int], n_intents: int = 30,
             answer_mode: str = "label", cfg: TrainConfig | None = None, data_dir: str | Path = "data",
             paraphrases_path: str | Path | None = None, use: int | None = None) -> Path:
    """Run every (k, seed) cell, appending one JSON line per cell; cells already in the file are skipped."""
    cache = Cache.load(paraphrases_path) if paraphrases_path else None
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {json.loads(line)["key"] for line in out.read_text().splitlines()} if out.exists() else set()
    data = load_banking77(data_dir)
    cfg = cfg or TrainConfig(base=base)
    cfg = TrainConfig(**{**asdict(cfg), "base": base})
    enc = Encoder.load(base).to(pick_device(cfg.device))
    for k in ks:
        for seed in seeds:
            # the "all questions" ceiling has plenty of real data, so the style augmentation is switched off
            c = TrainConfig(**{**asdict(cfg), "n_aug": 0, "max_steps": 150}) if k is None else cfg
            key = cell_key(base, n_intents, k, seed, answer_mode, c, 0 if cache is None else (use or cache.n))
            if key in done:
                continue
            res = run_scenario(data, seed, n_intents, k, c, answer_mode, enc, cache, use)
            res["key"] = key
            with out.open("a") as f:
                f.write(json.dumps(res, default=lambda o: None if isinstance(o, float) and np.isnan(o) else o) + "\n")
            s = res["systems"]
            extra = f" +{res['llm_paraphrases']} LLM" if cache is not None else ""
            print(f"[{base} n={n_intents} k={k}{extra} seed={seed}] hit@1 bm25 {s['bm25']['hit@1']:.3f} | untuned {s['base']['hit@1']:.3f} | tuned {s['tuned']['hit@1']:.3f}"
                  f" | steps {res['training']['steps']} ({res['training']['train_seconds']:.0f}s) | beat untuned on dev: {res['training']['beat_untuned_on_dev']}", flush=True)
    return out


def _cell(vals: list[float]) -> str:
    mean, half = metrics.mean_ci(vals)
    return f"{mean:.3f}" if np.isnan(half) else f"{mean:.3f} ± {half:.3f}"


def _label(r: dict) -> str:
    k = "all" if r["k"] is None else str(r["k"])
    return f"{k} + {r['llm_paraphrases']} LLM" if r.get("llm_paraphrases") else k


def _paired(rows: list[dict]) -> list[str]:
    """Same intents and same first phrasing per seed: no extra phrasings, LLM-written ones, and real ones."""
    by = {}
    for r in rows:
        by.setdefault((r["base"], r["n_intents"], r["answer_mode"], r["seed"]), {})[_label(r)] = r
    llm = sorted({lab for d in by.values() for lab in d if "LLM" in lab})
    out = []
    for lab in llm:
        real = next((x for x in ("8", "4", "2") if x == str(1 + int(lab.split("+")[1].split()[0]))), None)
        seeds = [(key, d) for key, d in sorted(by.items()) if "1" in d and lab in d and (real is None or real in d)]
        if not seeds:
            continue
        cols = ["1 phrasing", lab] + ([f"{real} real phrasings"] if real else [])
        out += [f"### LLM-written vs real phrasings (same intents, same first phrasing and same test questions per seed): {lab}", "",
                "Hit@1, untuned → tuned.", "", "| Seed | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        gains = {c: [] for c in cols}
        for (_, _, _, seed), d in seeds:
            cells = []
            for c in cols:
                r = d["1" if c == "1 phrasing" else lab if c == lab else real]
                u, t = r["systems"]["base"]["hit@1"], r["systems"]["tuned"]["hit@1"]
                gains[c].append((u, t))
                cells.append(f"{u:.3f} → {t:.3f}")
            out.append(f"| {seed} | " + " | ".join(cells) + " |")
        ref = np.mean([u for u, _ in gains["1 phrasing"]])
        out.append("| mean | " + " | ".join(f"{np.mean([u for u, _ in gains[c]]):.3f} → {np.mean([t for _, t in gains[c]]):.3f}" for c in cols) + " |")
        out += ["", "Tuned Hit@1 minus the untuned model with a single phrasing (what the extra phrasings and the tuning together are worth): "
                + ", ".join(f"{c} {np.mean([t for _, t in gains[c]]) - ref:+.3f}" for c in cols) + ".", ""]
    return out


def summarize(paths: str | Path | list) -> str:
    paths = [paths] if isinstance(paths, (str, Path)) else paths
    rows = [json.loads(line) for path in paths for line in Path(path).read_text().splitlines() if line.strip()]
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        order = (10**6 if r["k"] is None else r["k"]) + (0.5 if r.get("llm_paraphrases") else 0)
        groups.setdefault((r["base"], r["n_intents"], r["answer_mode"], order, _label(r)), []).append(r)
    out = []
    for metric in ("hit@1", "mrr@10"):
        out += [f"### {metric} on unseen test questions (mean ± 95% interval over runs)", "",
                "| Base | Entries | Answer text | Phrasings per entry | Runs | " + " | ".join(LABELS[s] for s in ("bm25", "base", "tuned", "hybrid_base", "hybrid_tuned")) + " | Tuned − untuned | Tuned beat untuned on dev |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for (base, n, mode, _, kk), rs in sorted(groups.items()):
            cols = [_cell([r["systems"][s][metric] for r in rs]) for s in ("bm25", "base", "tuned", "hybrid_base", "hybrid_tuned")]
            diff = [r["systems"]["tuned"][metric] - r["systems"]["base"][metric] for r in rs]
            wins = sum(r["training"]["beat_untuned_on_dev"] for r in rs)
            out.append(f"| {base} | {n} | {mode} | {kk} | {len(rs)} | " + " | ".join(cols) + f" | {_cell(diff)} | {wins}/{len(rs)} |")
        out.append("")
    out += _paired(rows)
    out += ["### Answering with thresholds chosen on dev only", "",
            "Direct answers must be right at least 95% of the time (90% confidence bound on dev); the floor keeps 95% of correctly answered dev questions. "
            "Out-of-scope questions are test questions about topics that are not in the knowledge base.", "",
            "| Base | Phrasings | Model | Runs with a certified direct threshold | Direct precision in those runs (answerable questions) | Direct precision in those runs (out-of-scope counted wrong) | Answerable questions answered directly (all runs) | Out-of-scope kept off direct | Out-of-scope sent to \"don't know\" | AUROC answerable vs out-of-scope |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for (base, n, mode, _, kk), rs in sorted(groups.items()):
        if mode != "label":
            continue
        for model, label in (("base", "Untuned"), ("tuned", "Tuned")):
            a = [r["abstention"][model] for r in rs if r["abstention"]]
            if not a:
                continue
            certified = [x for x in a if x["in_direct_acc"] == x["in_direct_acc"] and x["in_direct_acc"] is not None]
            prec = _cell([x["in_direct_acc"] for x in certified]) if certified else "n/a"
            allp = _cell([x["direct_precision"] for x in certified]) if certified else "n/a"
            kept = [1 - x["oos_direct_share"] for x in a]
            out.append(f"| {base} | {kk} | {label} | {len(certified)}/{len(a)} | {prec} | {allp} | {_cell([x['direct_coverage'] for x in a])} | {_cell(kept)} | {_cell([x['oos_none_share'] for x in a])} | {_cell([x['auroc_vs_oos'] for x in a])} |")
    return "\n".join(out) + "\n"
