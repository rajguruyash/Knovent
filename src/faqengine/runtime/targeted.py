"""STAGE 6: TARGETED RETRIEVAL. At most ONE additional pass per question, and only for a gap the sufficiency assessment says a search could close (a named entity, condition, attribute or scope
that no accepted evidence mentions). It reuses the retrieval it is given (`search`, the same hybrid search, bound to the same business and the same freshness rules), asks for what is missing
by name, drops what was already found, never repeats a query that was already asked, and puts no quota on any kind of source. What it finds goes through the same normalisation and validation
as everything else, with evidence ids that continue the numbering (earlier ids never change). Standard library only.
"""
from __future__ import annotations

MAX_QUERIES = 2  # the one pass may ask for at most this many different things


def second_pass(gap_queries: list[str], asked: set[str], search, known_entry_ids: set[str]) -> tuple[list[dict], list[str]]:
    """(new hits, the queries actually run). `asked` holds the queries already used (the customer's own question included); it is updated."""
    run: list[str] = []
    new: list[dict] = []
    seen = set(known_entry_ids)
    for q in gap_queries:
        key = " ".join(q.lower().split())
        if not key or key in asked or len(run) >= MAX_QUERIES:
            continue
        asked.add(key)
        run.append(q)
        faq, passages, _ = search(q)
        for h in [*faq, *passages]:
            eid = h.get("entry_id")
            if eid and eid not in seen:
                seen.add(eid)
                new.append({**h, "found_by": "targeted"})
    return new, run
