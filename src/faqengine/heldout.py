"""Independent, typed held-out questions, and the checks that keep them independent of what the engine trained on.

A held-out set is only worth something if nothing in it was derived from a training phrasing. Two protections:
  * the questions are written separately (not generated from training examples), and
  * every one is compared with every training phrasing, and any near-duplicate (the same words, a typo of one, a
    light rewording) is removed from the test and listed, so alternate phrasings cannot leak across the split.

File format (CSV, header required):  query, entry_id, type, split (optional), source (optional)
  entry_id  the right entry; several separated by | when more than one answer is acceptable (type ambiguous);
            empty for questions the FAQ cannot answer (type out-of-scope-*)
  type      paraphrase | answer-terms | typo | short | ambiguous | out-of-scope-near | out-of-scope-far
  split     dev (used to stop training, choose the model and set thresholds) or test (reported only; default)
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from .kb import KB, Queries, clean

IN_SCOPE_TYPES = ("paraphrase", "answer-terms", "typo", "short", "multi-part", "multilingual", "question", "ambiguous")
OOS_TYPES = ("out-of-scope-near", "out-of-scope-far", "out-of-scope")
TYPES = IN_SCOPE_TYPES + OOS_TYPES
SINGLE_TYPES = ("paraphrase", "answer-terms", "typo", "short", "multilingual", "question")  # questions with exactly one right entry (when they name one)
SET_TYPES = ("ambiguous", "multi-part")  # may name several entries: any one is fine (ambiguous), or all are wanted (multi-part)
COLUMNS = {  # the names other people give the columns of a test file
    "query": ("query", "question", "q", "utterance", "prompt", "text"),
    "entry_id": ("entry_id", "expected_entry", "expected_entry_id", "expected_id", "gold_entry", "gold_id", "gold", "expected_source_or_answer", "expected_source", "expected"),
    "type": ("type", "question_type", "kind"),
    "topic": ("category", "topic", "group"),
    "behavior": ("expected_behavior", "expected_behaviour", "behavior", "behaviour"),
    "split": ("split", "set", "subset"),
}
LABELS = {
    "paraphrase": "Paraphrases (conversational wording, synonyms)",
    "answer-terms": "Answer-word questions (the answer's own terms)",
    "typo": "Typos and misspellings",
    "short": "Short or incomplete questions, abbreviations",
    "multi-part": "Multi-part questions (every part has to be found)",
    "multilingual": "Other languages and Hinglish (asked of this knowledge base)",
    "question": "Questions (kind not labelled)",
    "ambiguous": "Ambiguous questions (more than one entry is acceptable)",
    "out-of-scope-near": "Related but unanswered (about your subject, not in the knowledge base)",
    "out-of-scope-far": "Completely unrelated",
    "out-of-scope": "Out of scope (not labelled near or unrelated)",
}


def normalize(text: str) -> str:
    """Lower case, punctuation removed; letters, digits and any currency symbol are kept, so a price stays part of the question."""
    kept = "".join(c if (c.isalnum() or c.isspace() or unicodedata.category(c) == "Sc") else " " for c in text.lower())
    return re.sub(r"\s+", " ", kept).strip()


def near_duplicate(a: str, b: str, char_ratio: float = 0.85, token_jaccard: float = 0.75) -> bool:
    """The same question up to punctuation, a typo or a light rewording."""
    na, nb = normalize(a), normalize(b)
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb and len(ta & tb) / len(ta | tb) >= token_jaccard:
        return True
    return SequenceMatcher(None, na, nb).ratio() >= char_ratio


def training_phrasings(kb: KB) -> list[str]:
    return [q for e in kb.entries for q in e.questions]


@dataclass
class HeldOut:
    queries: list[str]
    gold: list[frozenset]  # entry indices; empty for out-of-scope
    types: list[str]
    split: list[str]
    source: list[str]
    removed: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    val_kb: object | None = None  # when validation rows were added (generated from the FAQ, never from the file), the knowledge base they are scored against
    warnings: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.queries)

    def where(self, split: str | None = None, types: tuple | None = None) -> list[int]:
        return [i for i in range(len(self)) if (split is None or self.split[i] == split) and (types is None or self.types[i] in types)]

    def single(self, split: str, remap: dict | None = None) -> Queries:
        """Questions with exactly one right entry, for the single-gold code (training dev set). `remap` renumbers entries
        (for example into the FAQ-only knowledge base training uses) and drops questions whose entry is not in it."""
        rows = [i for i in self.where(split, SINGLE_TYPES) if len(self.gold[i]) == 1 and (remap is None or next(iter(self.gold[i])) in remap)]
        gold = [next(iter(self.gold[i])) for i in rows]
        return Queries([self.queries[i] for i in rows], np.array([remap[g] if remap else g for g in gold], dtype=int))

    def write_csv(self, path: str | Path, kb: KB) -> None:
        """The checked questions as a plain file (query, entry_id, type, split): repeated questions gone, every expectation an entry id or empty (abstain)."""
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["query", "entry_id", "type", "split"])
            for q, g, t, sp in zip(self.queries, self.gold, self.types, self.split):
                w.writerow([q, "|".join(kb.entries[i].id for i in sorted(g)), t, sp])

    def counts(self) -> dict:
        st = self.stats or {}
        out: dict = {"total": len(self), "removed_for_leakage": len(self.removed), "by_type": {}, "by_split": {"dev": 0, "test": 0},
                     "rows_read": st.get("rows", len(self)), "unique_questions": len(self) + len(self.removed), "exact_duplicates_removed": len(st.get("exact_duplicates", [])),
                     "near_duplicates": len(st.get("near_duplicates", [])), "abstentions": st.get("abstentions", sum(t in OOS_TYPES for t in self.types)),
                     "expected_entries": len({e for g in self.gold for e in g}), "topics": st.get("topics", {})}
        for t, s in zip(self.types, self.split):
            out["by_type"].setdefault(t, {"dev": 0, "test": 0})[s] += 1
            out["by_split"][s] += 1
        return out


def _col(row: dict, name: str) -> str:
    for alias in COLUMNS[name]:
        if row.get(alias):
            return row[alias]
    return ""


def _resolve(label: str, kb: KB) -> list[int]:
    """Entry indices a label names: an id, a heading or question written out ("Handbook: opening schedule", "FAQ: studio location" is NOT enough:
    only exact ids, exact questions and exact headings are trusted, so a guess cannot quietly become the gold answer)."""
    label = label.strip()
    if label in kb.index:
        return [kb.index[label]]
    bare = normalize(re.sub(r"^(faq|handbook|document|doc)\s*:\s*", "", label, flags=re.I))
    hits = [i for i, e in enumerate(kb.entries) if bare and bare in (normalize(e.question), normalize(e.id))]
    return hits


def _suggest(label: str, kb: KB) -> str:
    """The entries that share the most words with a label (a "FAQ:" label looks only at FAQ entries, a "Handbook:" label at passages), to help name the right id."""
    kind = "faq" if re.match(r"\s*faq\s*:", label, re.I) else "passage" if re.match(r"\s*(handbook|document|doc)\s*:", label, re.I) else None
    words = set(normalize(re.sub(r"^(faq|handbook|document|doc)\s*:\s*", "", label, flags=re.I)).split()) - {"and", "the", "of", "a", "to"}
    scored = sorted(((3 * len(words & set(normalize(e.question).split())) + len(words & set(normalize(e.answer).split())), e.id, e.question)
                     for e in kb.entries if kind is None or e.kind == kind), reverse=True)
    return "; ".join(f"{i} \"{q[:50]}\"" for n, i, q in scored[:2] if n)


def load_heldout(path: str | Path, kb: KB, train: list[str] | None = None, drop_leaks: bool = True, labels: dict[str, str] | None = None) -> HeldOut:
    """Parse the file, check every row, and remove questions that are near-duplicates of a training phrasing.

    Columns are found under several names (query/question, entry_id/expected_entry/..., type, split, expected_behavior). Every row needs a valid
    expectation: the entry (or entries) it should find, or abstention. Repeated questions are removed and counted, never kept to pad the set;
    the same question with two different expectations is an error. `labels` maps section names used in the file ("FAQ: studio location") to entry ids."""
    labels = {normalize(k): v for k, v in (labels or {}).items()}
    train = training_phrasings(kb) if train is None else train
    ids = kb.index
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    header = text.split("\n", 1)[0]
    delimiter = max(",;\t|", key=lambda d: (header.count(d), d == ","))
    rows = [{(k or "").strip().lower().replace(" ", "_"): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    if rows and not any(a in rows[0] for a in COLUMNS["query"]):
        raise ValueError("the held-out file needs a column with the questions (query or question) and one saying what each should find (entry_id, or expected_behavior: abstain)")
    ho = HeldOut([], [], [], [], [])
    ho.stats = {"rows": 0, "empty": 0, "exact_duplicates": [], "near_duplicates": [], "topics": {}, "abstentions": 0}
    seen: dict[str, tuple] = {}
    problems: list[str] = []
    for n, r in enumerate(rows, start=2):
        q = clean(_col(r, "query"))
        if not q:
            ho.stats["empty"] += 1
            continue
        ho.stats["rows"] += 1
        behavior, split = _col(r, "behavior").lower(), (_col(r, "split") or "test").lower()
        label, t = _col(r, "entry_id"), _col(r, "type").lower().replace("_", "-")
        abstain = behavior.startswith("abstain") or label.lower().startswith(("abstain", "none", "oos", "-")) or t in OOS_TYPES
        refs: list[int] = []
        if not abstain:  # an abstaining row names no entry: whatever it says in that column is ignored
            for part in [x for x in re.split(r"[|;]", label) if x.strip()]:
                mapped = labels.get(normalize(part))
                got = [i for x in re.split(r"[|;,]", mapped) for i in _resolve(x, kb)] if mapped else _resolve(part, kb)
                if not got:
                    guess = _suggest(part, kb)
                    problems.append(f"row {n}: {part.strip()!r} is not the id, question or heading of any entry" + (f" (closest: {guess})" if guess else ""))
                refs += got
            if not refs and label == "":
                problems.append(f"row {n}: {q!r} has no expected entry and is not marked abstain")
        if not t:
            t = "out-of-scope" if abstain else "question"
        if t not in TYPES:
            raise ValueError(f"held-out row {n}: type {t!r} is not one of {', '.join(TYPES)}")
        if split not in ("dev", "test", "validation", "held-out"):
            raise ValueError(f"held-out row {n}: split must be dev or test, not {split!r}")
        if split in ("dev", "validation"):
            ho.warnings.append(f"row {n}: the split column says {split}, but held-out questions are only ever used to measure the engine, so it is treated as a test question") if not ho.stats.get("split_note") else None
            ho.stats["split_note"] = True
            split = "test"
        if t not in OOS_TYPES and not refs:
            continue  # the problem was recorded above
        refs = sorted(set(refs))
        if len(refs) > 1 and t not in SET_TYPES and t not in ("question", "multilingual"):
            problems.append(f"row {n}: only ambiguous and multi-part questions may name several entries")
        if t == "ambiguous" and len(refs) < 2:
            ho.warnings.append(f"row {n}: ambiguous question {q!r} names only one entry")
        key = normalize(q)
        expectation = (tuple(refs), t in OOS_TYPES)
        if key in seen:
            if seen[key] != expectation:
                problems.append(f"row {n}: {q!r} appears twice with different expectations")
            else:
                ho.stats["exact_duplicates"].append(q)
            continue
        seen[key] = expectation
        twin = next((o for o in ho.queries if near_duplicate(q, o, 0.92, 0.85)), None)
        if twin is not None:
            ho.stats["near_duplicates"].append({"query": q, "near": twin})
            ho.warnings.append(f"row {n}: {q!r} is almost the same question as {twin!r}; both are kept, but count them as one when reading the results")
        leak = next((p for p in train if near_duplicate(q, p)), None)
        if leak is not None and drop_leaks:
            ho.removed.append({"query": q, "type": t, "split": split, "matches_training_phrasing": leak})
            continue
        topic = _col(r, "topic")
        if topic:
            ho.stats["topics"][topic] = ho.stats["topics"].get(topic, 0) + 1
        ho.stats["abstentions"] += int(t in OOS_TYPES)
        ho.queries.append(q)
        ho.gold.append(frozenset(refs))
        ho.types.append(t)
        ho.split.append(split)
        ho.source.append(r.get("source", ""))
    if problems:
        raise ValueError("the held-out file has rows without a valid expectation:\n  " + "\n  ".join(problems[:12]) + (f"\n  ... and {len(problems) - 12} more" if len(problems) > 12 else "")
                         + "\nName the entry by its id (shown in the Files tab), or mark the row abstain.")
    return ho


def assign_splits(rows: list[dict], every: int = 3) -> None:
    """Deterministic, balanced: within each type, every `every`-th question (in a fixed order) goes to dev, the rest to test."""
    by_type: dict[str, list[dict]] = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    for t, items in by_type.items():
        for i, r in enumerate(sorted(items, key=lambda r: (r["entry_id"], r["query"]))):
            r["split"] = "dev" if i % every == every - 1 else "test"
