"""Query-style augmentation for training.

Customers do not type the FAQ question word for word: they drop punctuation, shorten it to keywords, make
typos. These transforms teach the model that style. They do not add new paraphrases (that needs an LLM or
real customer questions), so they cannot replace paraphrase diversity.
"""
from __future__ import annotations

import math
import random
import re

from .bm25 import _STOP

_WORD = re.compile(r"[\w']+")


def _words(q: str) -> list[str]:
    return _WORD.findall(q.lower())


def _no_punct(q: str, rng: random.Random) -> str:
    return " ".join(_words(q))


def _keywords(q: str, rng: random.Random) -> str:
    kept = [w for w in _words(q) if w not in _STOP]
    return " ".join(kept) if kept else ""


def _dropout(q: str, rng: random.Random) -> str:
    words = _words(q)
    if len(words) < 4:
        return ""
    kept = [w for w in words if rng.random() > 0.25]
    return " ".join(kept) if len(kept) >= 2 else ""


def _typo(q: str, rng: random.Random) -> str:
    words = _words(q)
    long = [i for i, w in enumerate(words) if len(w) >= 5]
    if not long:
        return ""
    for i in rng.sample(long, k=min(2, len(long))):
        w, j = words[i], rng.randrange(1, len(words[i]) - 1)
        words[i] = rng.choice([w[:j] + w[j + 1:], w[:j] + w[j + 1] + w[j] + w[j + 2:], w[:j] + w[j] + w[j:]])
    return " ".join(words)


def _head(q: str, rng: random.Random) -> str:
    words = _words(q)
    return " ".join(words[: math.ceil(len(words) * 0.6)]) if len(words) >= 5 else ""


def _tail(q: str, rng: random.Random) -> str:
    words = _words(q)
    return " ".join(words[-math.ceil(len(words) * 0.6):]) if len(words) >= 5 else ""


NAMES = {"no_punct": _no_punct, "keywords": _keywords, "dropout": _dropout, "typo": _typo, "head": _head, "tail": _tail}
TRANSFORMS = tuple(NAMES.values())
# When a FAQ has no real customer questions to test on, the model trains on one set of kinds and is checked on the
# other, so the check measures robustness to corruption it never practised.
TRAIN_KINDS = ("no_punct", "keywords", "dropout")
EVAL_KINDS = ("typo", "head", "tail")


def augment(question: str, rng: random.Random, n: int, kinds: tuple[str, ...] | None = None) -> list[str]:
    """Up to n distinct variants of a question, none equal to the original. `kinds` limits the transforms used."""
    out: list[str] = []
    seen = {question.strip().lower()}
    order = [NAMES[k] for k in kinds] if kinds else list(TRANSFORMS)
    rng.shuffle(order)
    for fn in order * (3 if kinds else 2):
        if len(out) >= n:
            break
        text = fn(question, rng).strip()
        if kinds and not any(w not in _STOP for w in _words(text)):
            continue  # restricted kinds are used to build checks: a variant with no content word could not be answered by anyone
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out
