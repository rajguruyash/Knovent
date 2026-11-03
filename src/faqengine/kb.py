"""The knowledge base: FAQ entries, the "views" that get embedded for each, and loading from CSV / JSON / JSONL."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ALT_SEP = "|"
LONG_ANSWER_CHARS = 500


def clean(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


@dataclass
class Entry:
    id: str
    questions: list[str]  # primary question first, then alternate phrasings
    answer: str = ""
    category: str | None = None
    kind: str = "faq"  # "faq": a question with its curated answer; "passage": a piece of a document (embedded and searched, used as evidence for answers, never returned as a stored FAQ answer)
    source: str = ""  # the file it came from
    valid_from: str | None = None  # ISO dates a source may state about itself (front-matter); see runtime/validity.py
    valid_until: str | None = None
    supersedes: str | None = None  # the earlier source this one replaces
    inactive: str | None = None  # why this is not current today (expired, replaced...); worked out when the knowledge is loaded, never saved
    record: dict | None = None  # a row of a table of values, as data: {table, entity, noun, subject, fields: [{header, key, label, value, text}]} (the passage's sentences say the same in words)

    @property
    def question(self) -> str:
        return self.questions[0]


@dataclass(frozen=True)
class View:
    text: str
    entry: int  # index into KB.entries
    kind: str  # "q": one question phrasing, "qa": primary question plus answer


def qa_text(question: str, answer: str) -> str:
    return f"{question} {answer}"


class KB:
    """Every entry is searched through several views and scores as its best view."""

    def __init__(self, entries: list[Entry], warnings: list[str] | None = None):
        if not entries:
            raise ValueError("the knowledge base has no entries")
        self.entries = entries
        self.warnings = warnings or []
        self.views: list[View] = []
        starts = []
        for i, e in enumerate(entries):
            starts.append(len(self.views))
            self.views += [View(q, i, "q") for q in e.questions]
            if e.answer:
                self.views.append(View(qa_text(e.question, e.answer), i, "qa"))
        self.view_starts = np.array(starts)
        self.view_entry = np.array([v.entry for v in self.views])
        self.index = {e.id: i for i, e in enumerate(entries)}

    def __len__(self) -> int:
        return len(self.entries)

    def entry_max(self, view_scores: np.ndarray) -> np.ndarray:
        """Collapse (queries x views) scores to (queries x entries)."""
        return np.maximum.reduceat(view_scores, self.view_starts, axis=-1)

    def faq_only(self) -> tuple["KB | None", list[int]]:
        """The curated question/answer entries alone (documents are searched, but training and answering directly use only these),
        and which index of this knowledge base each one has. None when there are no FAQ entries at all."""
        keep = [i for i, e in enumerate(self.entries) if e.kind == "faq"]
        return (KB([self.entries[i] for i in keep], list(self.warnings)) if keep else None), keep

    def views_of(self, entry: int) -> list[View]:
        end = self.view_starts[entry + 1] if entry + 1 < len(self.entries) else len(self.views)
        return self.views[self.view_starts[entry]:end]


def _entry(row: dict, n: int) -> Entry:
    questions: list[str] = []
    first = row.get("questions") if isinstance(row.get("questions"), list) else [row.get("question")]
    alts = row.get("alt_questions") or row.get("alternates") or []
    if isinstance(alts, str):
        alts = alts.split(ALT_SEP)
    for text in [*first, *alts]:
        text = clean(text)
        if text and text.lower() not in {q.lower() for q in questions}:
            questions.append(text)
    if not questions:
        raise ValueError(f"entry {n}: no question")
    return Entry(
        id=clean(row.get("id")) or f"faq-{n:03d}",
        questions=questions,
        answer=clean(row.get("answer")),
        category=clean(row.get("category")) or None,
    )


def _rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as f:
            return [{(k or "").strip().lower(): v for k, v in row.items()} for row in csv.DictReader(f)]
    if suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = next((data[k] for k in ("faqs", "entries", "items") if k in data), None)
        if not isinstance(data, list):
            raise ValueError(f"{path.name}: expected a list of entries (or an object with 'faqs', 'entries' or 'items')")
        return data
    raise ValueError(f"{path.name}: use .csv, .json or .jsonl")


def load_kb(path: str | Path | list) -> KB:
    """One FAQ file, or several files (FAQ tables and documents) as one knowledge base. See sources.py for the formats."""
    from .sources import load_sources

    return load_sources(path)


@dataclass
class Queries:
    texts: list[str]
    gold: np.ndarray  # entry index, or -1 for a question the knowledge base cannot answer

    def __len__(self) -> int:
        return len(self.texts)


def load_queries(path: str | Path, kb: KB) -> Queries:
    """CSV with columns query, entry_id. A blank, '-' or 'oos' entry_id marks an out-of-scope question."""
    texts, gold = [], []
    for n, row in enumerate(_rows(Path(path)), start=2):
        text = clean(row.get("query") or row.get("question"))
        if not text:
            continue
        ref = clean(row.get("entry_id") or row.get("id"))
        if ref.lower() in ("", "-", "oos"):
            gold.append(-1)
        elif ref in kb.index:
            gold.append(kb.index[ref])
        else:
            raise ValueError(f"{Path(path).name} row {n}: unknown entry_id {ref!r}")
        texts.append(text)
    return Queries(texts, np.array(gold, dtype=int))


def write_kb(entries: list[Entry], path: str | Path) -> None:
    """Save entries as .csv (alt_questions separated by '|') or .json."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        path.write_text(json.dumps([{"id": e.id, "question": e.question, "alt_questions": e.questions[1:], "answer": e.answer, "category": e.category,
                                     "kind": e.kind, "source": e.source,
                                     **{k: getattr(e, k) for k in ("valid_from", "valid_until", "supersedes", "record") if getattr(e, k)}} for e in entries], ensure_ascii=False, indent=1))
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "question", "answer", "category", "alt_questions"])
        for e in entries:
            w.writerow([e.id, e.question, e.answer, e.category or "", ALT_SEP.join(q.replace(ALT_SEP, "/") for q in e.questions[1:])])
