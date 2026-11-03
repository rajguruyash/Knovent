"""Word handling shared by the retrieval engine (keyword search) and the evidence engine, so that both read a text the same way. Standard library only."""
from __future__ import annotations

import re
import unicodedata

STOP = set("""a an the of in on at to for from by with about as into over under and or but if then than so is are was were be been being am do does did done can could will would
should may might must shall have has had i you he she it we they me my your our their his her its this that these those there here what which who whom whose when where why how
please tell know want need like get got let also just any some more most very much many yes no not happen happens happened time times kind type thing come comes came give gives gave offer offers provide provides include includes included ask asks asked""".split())

SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ed", "es", "s")


def stem(word: str) -> str:
    w = word.lower()
    for suffix in SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


STOP_STEMS = {stem(w) for w in STOP}


def terms(text: str) -> list[str]:
    return [t for t in (stem(w) for w in re.findall(r"[^\W_]+", text.lower()) if w not in STOP and (len(w) > 1 or w.isdigit())) if t not in STOP_STEMS]


MAX_QUERY_CHARS = 2000


def normalize_query(text: str) -> str:
    """A customer's question made ready for searching: Unicode composed (NFKC), control and zero-width characters dropped, whitespace collapsed, length capped. The case is kept
    (the model reads it); the keyword channel lowercases for itself."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = "".join(c for c in t if c in "\n\t " or unicodedata.category(c) not in ("Cc", "Cf"))
    return re.sub(r"\s+", " ", t).strip()[:MAX_QUERY_CHARS]
