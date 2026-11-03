"""The question-answering pipeline, with the three engines kept apart and this module as the only place that calls them in order:

    search(question)            retrieval.py   candidates with their scores                      (no model needed beyond the embedding model)
    quarantine                  safety.py      passages that address an AI are set aside and named
    evidence.assess             evidence.py    which parts of the question the candidates support, and how
    [assist]                    generate.py    OPTIONAL: other search queries when too little was found; the new candidates are assessed against the CUSTOMER'S question
    response.compose            response.py    what is said; OPTIONAL model wording, accepted only if verify.py passes it

With no language model every step above still runs. With one, it can only add wording or search terms; it never changes what the evidence engine decided.
"""
from __future__ import annotations

import inspect
import time
from datetime import date

try:
    from . import evidence as ev
    from . import generate, response, safety, structured, temporal
    from .textutil import normalize_query
except ImportError:  # an exported package puts runtime/ on the path
    import evidence as ev
    import generate
    import response
    import safety
    import structured
    import temporal
    from textutil import normalize_query


def _takes_day(search) -> bool:
    try:
        return "on" in inspect.signature(search).parameters
    except (TypeError, ValueError):
        return False


def _quarantine(hits: list[dict]) -> tuple[list[dict], list[dict]]:
    keep, held = [], []
    for h in hits:
        flags = safety.injection_flags(f"{h.get('question', '')}\n{h.get('answer', '')}")
        (held if flags else keep).append({**h, "injection_flags": flags} if flags else h)
    return keep, held


def _merge(base: list[dict], extra: list[dict]) -> list[dict]:
    seen = {h["entry_id"] for h in base}
    return base + [{**h, "rank": len(base) + k + 1, "found_by": "assist"} for k, h in enumerate(x for x in extra if x["entry_id"] not in seen)]


def candidate_view(a: dict) -> list[dict]:
    """The structured retrieval data for the candidates that were assessed, for diagnostics and attribution."""
    keys = ("rank", "entry_id", "kind", "source", "category", "location", "similarity_score", "keyword_score", "combined_score", "retrieval_method", "matched_kind", "found_by", "duplicates")
    return [{k: h.get(k) for k in keys if k in h} | {"question": h.get("question", "")} for h in a["pool"]]


def respond(question: str, search, sim=None, llm=None, similarity_floor: float = 0.0, sem_min: float = ev.SEM_MIN, as_of=None) -> dict:
    """`search(query) -> (faq_hits, passage_hits, diagnostics)`. `llm` is an LLM object (llm.py), a plain function, or None."""
    started = time.perf_counter()
    llm = generate.as_llm(llm)
    q = normalize_query(question)
    before = dict(llm.usage()) if llm is not None and hasattr(llm, "usage") else None
    asked_day = temporal.reference_day(q, as_of or date.today()) if temporal.dates_in(q) else None  # a question that names a day is about that day, whatever today is
    faq, passages, diag = search(q, on=asked_day) if asked_day and _takes_day(search) else search(q)
    (faq, held_f), (passages, held_p) = _quarantine(faq), _quarantine(passages)
    held = held_f + held_p
    stale = list(diag.get("inactive_matches", []))
    outdated = None
    if stale:  # a part that out-of-date material states in full and current material only comes near is not answered by the current material
        probe = ev.assess(q, [], [{**m, "kind": "passage"} for m in stale], sim, sem_min, diag.get("unknown_names"), as_of)
        outdated = {probe["parts"][p] for p in range(len(probe["parts"])) if any(g["class"] == "direct" and g["coverage"] >= 1.0 for g in probe["grid"][p])} or None
    a = ev.assess(q, faq, passages, sim, sem_min, diag.get("unknown_names"), as_of, outdated)
    model = (getattr(search, "model", None) or (lambda: None))()  # the rows of the business's tables, if the caller can give them
    solved = [(part, r) for part in a["parts"] if model is not None and (r := structured.solve(model, part))]
    if solved and not response.unspecific(q):
        left = [p for p in a["parts"] if p not in {s[0] for s in solved}]
        rest = None
        if left:
            a_rest = ev.assess("; ".join(left), faq, passages, sim, sem_min, diag.get("unknown_names"), as_of, outdated, multi=True)  # (still a part of a longer question)
            rest = response.compose(a_rest, llm=llm, similarity_floor=similarity_floor, sim=sim)
        out = response.from_structured(solved, model, a, rest, llm)
        out["retrieval"] = {"diagnostics": diag, "candidates": candidate_view(a), "quarantined": [{"id": h["entry_id"], "flags": h["injection_flags"]} for h in held],
                            "note": "similarity_score is a cosine between texts, not a probability; combined_score only orders candidates; neither is an answer confidence"}
        out["assist"] = None
        out["latency_ms"] = (time.perf_counter() - started) * 1000
        return out
    vague = response.unspecific(q)
    if vague:  # nothing can be said to answer a question that names nothing, whatever the similarity: it goes to a question back
        a = {**a, "chosen": [], "unanswered": a["parts"], "conflicts": [], "best_faq": None, "answer_sufficiency": 0.0, "evidence_support": 0.0, "evidence_class": "no_evidence"}
    assist = None
    if llm is not None and llm.can("assist") and a["unanswered"] and not a["conflicts"] and not vague:
        r = generate.assist(llm, q, a["unanswered"])
        assist = {"attempted": True, "queries": r["queries"], "failure": r["failure"], "improved": False, "added_candidates": 0}
        if r["queries"]:
            more_f, more_p = [], []
            for alt in r["queries"]:
                f2, p2, _ = search(alt)
                more_f += _quarantine(f2)[0]
                more_p += _quarantine(p2)[0]
            merged_f, merged_p = _merge(faq, more_f), _merge(passages, more_p)
            assist["added_candidates"] = (len(merged_f) - len(faq)) + (len(merged_p) - len(passages))
            b = ev.assess(q, merged_f, merged_p, sim, sem_min, diag.get("unknown_names"), as_of, outdated)  # judged against what the customer asked, not against the suggested queries
            if b["evidence_support"] > a["evidence_support"]:
                a, assist["improved"] = b, True
    out = response.compose(a, llm=llm, similarity_floor=similarity_floor, sim=sim)
    if stale and out["outcome"] in ("insufficient", "partial"):  # would the material that is no longer current have answered it? Say so, without giving its facts as if they held
        probe = ev.assess(q, [], [{**m, "kind": "passage"} for m in stale], sim, sem_min, diag.get("unknown_names"), as_of)
        used_stale = sorted({chosen[1] for chosen in probe["chosen"]})
        if used_stale:
            out["outdated"] = [{"source": stale[i]["source"], "reason": stale[i]["inactive"], "section": stale[i]["question"]} for i in used_stale]
            names = "; ".join(f"{o['source']} ({o['reason']})" for o in out["outdated"])
            out["message"] = (out.get("message") or "") + f" The only information I found on this is out of date: {names}. I have not used it."
    out["retrieval"] = {"diagnostics": diag, "candidates": candidate_view(a), "quarantined": [{"id": h["entry_id"], "flags": h["injection_flags"]} for h in held],
                        "note": "similarity_score is a cosine between texts, not a probability; combined_score only orders candidates; neither is an answer confidence"}
    out["assist"] = assist
    if before is not None:
        after = llm.usage()
        out["llm"]["usage"] = {k: round(after.get(k, 0) - before.get(k, 0), 4) for k in ("calls", "tokens_in", "tokens_out", "seconds") if k in after}
        if after.get("cost") is not None:
            out["llm"]["usage"]["cost"] = round((after["cost"] or 0) - (before.get("cost") or 0), 6)
    out["latency_ms"] = (time.perf_counter() - started) * 1000
    return out
