"""Questions generated from a FAQ alone, for checking an engine when there are no real customer questions.

None of this is customer traffic, and it must never be presented as accuracy. It is a sanity check that can
catch real problems (an entry that cannot be found, two entries that are easily confused, an engine that
answers questions about the weather) but cannot certify that real customers will be understood. The real
test is a person trying questions and approving or rejecting what comes back.

What is generated, all from the FAQ file:
  held-out phrasing   an alternate way of asking, removed from training and from the index while checking
                      (only for entries that have 2 or more phrasings)
  typo or shortened   the question with typos, or cut down to its start or end, using kinds of corruption the
                      model was not trained on
  answer words        keywords taken from the answer, which tests whether the answer's wording leads back to the entry
  off-topic           questions no FAQ should answer (weather, trivia, jokes): the engine should say "not in the FAQ"
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

import numpy as np

from .augment import EVAL_KINDS, augment
from .bm25 import _STOP
from .kb import KB, Entry, Queries, clean

HELD_OUT, SHORTENED, ANSWER_WORDS, OFF_TOPIC_SOURCE = "held-out phrasing", "typo or shortened", "answer words", "off-topic"
IN_SCOPE_SOURCES = (HELD_OUT, SHORTENED, ANSWER_WORDS)
DEV_OFF_TOPIC = 12

OFF_TOPIC = [
    "what is the weather like today", "will it rain tomorrow", "who won the football match last night", "when is the next olympic games",
    "what is the capital of france", "how tall is mount everest", "how many people live in the world", "tell me a joke",
    "can you write me a poem about the sea", "what is two plus two", "what is the square root of 144", "what is the speed of light",
    "why is the sky blue", "how far away is the moon", "how do i boil an egg", "what is a good recipe for pancakes",
    "how do i bake sourdough bread", "who painted the mona lisa", "who wrote romeo and juliet", "what is your favourite colour",
    "how do i learn to play the guitar", "what is the best movie of all time", "who is the richest person alive", "how old is the universe",
    "what is the meaning of life", "how do i say thank you in japanese", "what animal is the fastest", "who invented the telephone",
    "what is the tallest building in the world", "how many legs does a spider have",
]


GENERIC = set("what is the an of in on at to do does did how many much who whom whose which when where why will would can could should you me my your we our it its are was "
              "were like today tomorrow last next best good all time world way make get have has had with for from about and not".split())
WORD = re.compile(r"[^\W\d_]{3,}")


def usable_probes(entries: list[Entry]) -> tuple[list[str], list[str]]:
    """The generic off-topic questions that really are off topic for this knowledge base, and the ones left out. A FAQ about cooking, geography or
    the weather answers some of them: a question the FAQ can answer must not be used to check that it says "not in the FAQ"."""
    vocab = {w for e in entries for w in WORD.findall(f"{' '.join(e.questions)} {e.answer}".lower())}
    keep, left_out = [], []
    for probe in OFF_TOPIC:
        (left_out if {w for w in WORD.findall(probe) if w not in GENERIC} & vocab else keep).append(probe)
    return keep, left_out


@dataclass
class SelfCheck:
    kb: KB  # the FAQ without the held-out phrasings: train on it and search it while checking
    dev: Queries
    test: Queries
    dev_source: list[str]
    test_source: list[str]
    held_out: int  # phrasings taken out of the index
    dropped_probes: list[str] = field(default_factory=list)  # generic off-topic questions this FAQ can answer, so they were not used

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.test_source:
            out[s] = out.get(s, 0) + 1
        return out


def answer_words(answer: str, question_text: str, limit: int = 6) -> str:
    """Up to `limit` content words of the answer that the question does not already use; '' if fewer than 3."""
    own = set(re.findall(r"[a-z0-9']+", question_text.lower()))
    words, seen = [], set()
    for w in re.findall(r"[a-z0-9][a-z0-9'-]+", answer.lower()):
        if w in _STOP or w in own or w in seen or w.isdigit():
            continue
        seen.add(w)
        words.append(w)
    return " ".join(words[:limit]) if len(words) >= 3 else ""


def build_selfcheck(kb: KB, seed: int = 0, per_entry: int = 2) -> SelfCheck:
    """Deterministic for a given FAQ and seed."""
    entries: list[Entry] = []
    dev_t, dev_g, dev_s, test_t, test_g, test_s = [], [], [], [], [], []
    known = {q.lower() for e in kb.entries for q in e.questions}
    taken: set[str] = set()
    held = 0

    def add(texts, golds, sources, text, gold, source, bucket_taken):
        key = text.lower()
        if text and key not in known and key not in bucket_taken:
            bucket_taken.add(key)
            texts.append(text)
            golds.append(gold)
            sources.append(source)
            return True
        return False

    for i, e in enumerate(kb.entries):
        questions = list(e.questions)
        held_test = held_dev = None
        if len(questions) >= 3:
            held_test, held_dev = questions.pop(), questions.pop()
        elif len(questions) == 2:
            held_test = questions.pop()
        entries.append(Entry(e.id, questions, e.answer, e.category, e.kind, e.source))
        for text, bucket in ((held_test, "test"), (held_dev, "dev")):
            if text:
                held += 1
                if bucket == "test":
                    test_t.append(text), test_g.append(i), test_s.append(HELD_OUT)
                else:
                    dev_t.append(text), dev_g.append(i), dev_s.append(HELD_OUT)
        rng = random.Random(f"{seed}:{e.id}")
        variants = augment(questions[0], rng, 2 * per_entry, EVAL_KINDS)
        for text in variants[:per_entry]:
            add(dev_t, dev_g, dev_s, text, i, SHORTENED, taken)
        for text in variants[per_entry:]:
            add(test_t, test_g, test_s, text, i, SHORTENED, taken)
        words = answer_words(e.answer, " ".join(e.questions))
        if words:
            add(test_t, test_g, test_s, words, i, ANSWER_WORDS, taken)

    # the first dozen set the "not in the FAQ" floor, the rest measure it: the same questions must not do both
    probes, left_out = usable_probes(entries)
    n_dev = DEV_OFF_TOPIC if len(probes) >= 2 * DEV_OFF_TOPIC else round(0.4 * len(probes))  # a FAQ that overlaps with many of them still gets both halves
    for j, text in enumerate(probes):
        if j < n_dev:
            dev_t.append(text), dev_g.append(-1), dev_s.append(OFF_TOPIC_SOURCE)
        else:
            test_t.append(text), test_g.append(-1), test_s.append(OFF_TOPIC_SOURCE)
    return SelfCheck(KB(entries), Queries(dev_t, np.array(dev_g, dtype=int)), Queries(test_t, np.array(test_g, dtype=int)), dev_s, test_s, held, left_out)
