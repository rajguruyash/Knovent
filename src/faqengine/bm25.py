"""BM25 keyword baseline: the thing a trained model has to beat, and half of the hybrid system."""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

import numpy as np

from .kb import KB

_STOP = frozenset(
    "a an the is are was were be been being am do does did have has had i me my we our you your it its of to in on at for "
    "from by with about as and or but if so than that this these those what which who whom how when where why can could "
    "will would should shall may might must not no yes please there here also just".split()
)
_WORD = re.compile(r"[a-z0-9]+")


def _stem_fn():
    try:
        import snowballstemmer

        return snowballstemmer.stemmer("english").stemWord
    except ImportError:  # keeps the baseline usable without the optional package
        return lambda w: w[:-1] if len(w) > 3 and w.endswith("s") else w


_stem = _stem_fn()


def tokenize(text: str) -> list[str]:
    return [_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        tokens = [tokenize(d) for d in docs]
        self.n = len(docs)
        self.lengths = np.array([len(t) for t in tokens], dtype=float)
        self.avg = max(self.lengths.mean(), 1e-9)
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, toks in enumerate(tokens):
            for term, tf in Counter(toks).items():
                self.postings[term].append((i, tf))
        self.idf = {t: math.log((self.n - len(p) + 0.5) / (len(p) + 0.5) + 1.0) for t, p in self.postings.items()}

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(self.n)
        for term in set(tokenize(query)):
            if term not in self.postings:
                continue
            idf = self.idf[term]
            for i, tf in self.postings[term]:
                norm = 1 - self.b + self.b * self.lengths[i] / self.avg
                out[i] += idf * tf * (self.k1 + 1) / (tf + self.k1 * norm)
        return out


def bm25_entry_scores(kb: KB, queries: list[str]) -> np.ndarray:
    """(queries x entries): BM25 over every view, each entry scored by its best view."""
    index = BM25([v.text for v in kb.views])
    return kb.entry_max(np.stack([index.scores(q) for q in queries]))
