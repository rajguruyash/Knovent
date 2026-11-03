"""The questions every configuration is run on: the same questions, in the same order, for all of them.

Pools (a question is in exactly one):
  dev     used to choose the hybrid blend and the cross-encoder model
  test    reported; never used to choose anything
  locked  the locked evidence benchmark (examples/evidence/locked.csv): reported once for the chosen model, never used to choose anything
Sources: the Pawfect held-out questions (split by a hash of the question), the six domain examples and the bike-shop example (their own dev/test split), and the evidence sets
(dev and regression rows are dev, locked rows are locked). Only questions with right entries are used: retrieval has nothing to say about "not in the knowledge base".
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

from faqengine.heldout import IN_SCOPE_TYPES, load_heldout
from faqengine.kb import KB, load_kb

ROOT = Path(__file__).resolve().parents[2]
EX = ROOT / "examples"


@dataclass
class Dataset:
    name: str
    kb: KB
    queries: list[str]
    gold: list[frozenset]
    category: list[str]
    pool: list[str]


def _split_of(path: Path) -> dict[str, str]:
    """The dev/test column some example files carry (the loader ignores it, because held-out files are test-only; here it picks the hybrid blend and the model)."""
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    return {r["query"].strip().lower(): (r.get("split") or "test").strip().lower() for r in rows if r.get("query")}


def _from_heldout(name: str, kb: KB, path: Path, split_by_hash: bool = False, labels: dict | None = None) -> Dataset:
    ho = load_heldout(path, kb, labels=labels)
    split = {} if split_by_hash else _split_of(path)
    d = Dataset(name, kb, [], [], [], [])
    for i, q in enumerate(ho.queries):
        if ho.types[i] not in IN_SCOPE_TYPES or not ho.gold[i]:
            continue
        d.queries.append(q)
        d.gold.append(ho.gold[i])
        d.category.append("paraphrase" if ho.types[i] == "question" else ho.types[i])  # the Pawfect file does not label the kind: its questions are rewordings of FAQ and handbook content
        d.pool.append(("dev" if int(hashlib.md5(q.encode()).hexdigest(), 16) % 2 == 0 else "test") if split_by_hash else ("dev" if split.get(q.strip().lower()) == "dev" else "test"))
    return d


def _from_evidence(name: str, kb: KB, sets: dict[str, str]) -> Dataset:
    d = Dataset(name, kb, [], [], [], [])
    for file, pool in sets.items():
        for r in csv.DictReader((EX / "evidence" / file).open(encoding="utf-8")):
            cites = [c for c in (r.get("cite") or "").split(";") if c]
            if r["expected_outcome"] in ("faq", "grounded", "partial") and cites and all(c in kb.index for c in cites):
                d.queries.append(r["question"])
                d.gold.append(frozenset(kb.index[c] for c in cites))
                d.category.append(r["category"])
                d.pool.append(pool)
    return d


def _banking77(per_intent_dev: int = 3, per_intent_test: int = 6, seed: int = 0) -> Dataset:
    """The large, hard one: 77 real banking intents as FAQ entries (4 real customer phrasings each), asked real customer utterances that are in none of the phrasings.
    Many intents are near neighbours (card arrival / card delivery estimate / card not working), so there are real hard negatives, and the top 20 is a quarter of the
    knowledge base. (Entries carry the generic answer bench.py uses: "This answers questions about <intent>".)"""
    import random

    from faqengine.bench import build_scenario, load_banking77

    sc = build_scenario(load_banking77(ROOT / "data"), seed, 77, 4, "label")
    rng = random.Random(seed)
    d = Dataset("banking77", sc.kb, [], [], [], [])
    for pool, q, per in (("dev", sc.dev, per_intent_dev), ("test", sc.test, per_intent_test)):
        by: dict[int, list[str]] = {}
        for text, g in zip(q.texts, q.gold):
            if g >= 0:
                by.setdefault(int(g), []).append(text)
        for g in sorted(by):
            for text in rng.sample(by[g], min(per, len(by[g]))):
                d.queries.append(text)
                d.gold.append(frozenset({g}))
                d.category.append("customer utterance")
                d.pool.append(pool)
    return d


def build() -> list[Dataset]:
    import json

    out = []
    pk = load_kb([EX / "pawfect" / "pawfect_faq.csv", EX / "pawfect" / "pawfect_handbook.md"])
    out.append(_from_heldout("pawfect", pk, EX / "pawfect" / "heldout.csv", split_by_hash=True, labels=json.loads((EX / "pawfect" / "labels.json").read_text())))
    for name in sorted(p.name for p in (EX / "domains").iterdir() if p.is_dir()):
        out.append(_from_heldout(name, load_kb(EX / "domains" / name / "faq.csv"), EX / "domains" / name / "heldout.csv"))
    out.append(_from_heldout("bike-shop", load_kb(ROOT / "src" / "faqengine" / "studio" / "static" / "example-bike-shop.csv"), EX / "bike-shop" / "heldout.csv"))
    ek = load_kb([EX / "evidence" / "pawfect_faq.csv", EX / "evidence" / "pawfect_handbook.md", EX / "evidence" / "policy_update.md"])
    out.append(_banking77())
    out.append(_from_evidence("evidence", ek, {"dev.csv": "dev", "regression.csv": "dev", "locked.csv": "locked"}))
    return out
