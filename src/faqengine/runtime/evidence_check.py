"""STAGES 3 AND 4: EVIDENCE NORMALISATION AND VALIDATION.

`normalize` turns what retrieval found into Evidence items with ids (E1, E2 ...) made here, before any model sees anything, and never changed afterwards. Every item keeps where it came from:
tenant, source, kind, location, heading, content, validity and how it was retrieved.

`validate` decides, for each item, whether it may be CITED: accepted (it states something a requirement of the question asks for), context (it is about the same thing but does not state what is
asked: a related claim, another scope), or rejected (another business's, expired, not yet valid, superseded, or about something else). A similarity score is never support: support is a
sentence that states the requirement (evidence.classify), judged without any retrieval score. Standard library only; no model.
"""
from __future__ import annotations

import re
from datetime import date

try:
    from . import evidence as ev
    from .contracts import Evidence, EvidenceCheck, QueryPlan
    from .validity import inactive_reasons
except ImportError:  # an exported package puts runtime/ on the path
    import evidence as ev
    from contracts import Evidence, EvidenceCheck, QueryPlan
    from validity import inactive_reasons


def normalize(hits: list[dict], tenant_id: str | None, start: int = 1, known: dict[str, str] | None = None) -> list[Evidence]:
    """Evidence items from retrieved hits, in the order given, numbered from `start`. An entry already numbered (`known`: entry id -> evidence id) keeps its id and is not made twice."""
    out: list[Evidence] = []
    n = start
    seen = set(known or {})
    for h in hits:
        eid = h.get("entry_id") or h.get("id")
        if not eid or eid in seen:
            continue
        seen.add(eid)
        kind = h.get("kind", "faq")
        record = h.get("record")
        heading = h.get("question", "") or ""
        source = h.get("source") or ("FAQ" if kind == "faq" else "document")
        meta = tuple(sorted({k: v for k, v in {"category": h.get("category"), "table": (record or {}).get("table"), "entity": (record or {}).get("entity"), "duplicates": len(h.get("duplicates") or [])
                                              or None}.items() if v}.items()))
        validity = tuple(sorted({k: v for k, v in {"valid_from": h.get("valid_from"), "valid_until": h.get("valid_until"), "supersedes": h.get("supersedes"), "inactive": h.get("inactive")}.items() if v}.items()))
        retrieval = tuple(sorted({k: v for k, v in {"method": h.get("retrieval_method"), "score": h.get("combined_score", h.get("score")), "similarity": h.get("similarity_score", h.get("score")), "rank": h.get("rank"), "found_by": h.get("found_by")}.items()
                                  if v is not None}.items()))
        out.append(Evidence(id=f"E{n}", tenant_id=h.get("tenant_id", tenant_id), entry_id=str(eid), source_id=str(h.get("source_id") or source), source_type="row" if record else kind, source_name=source,
                            location=(h.get("location") or {}).get("source", source) + (f" › {heading}" if kind != "faq" and heading else ""), heading=heading, content=h.get("answer", "") or "",
                            metadata=meta, validity=validity, retrieval=retrieval))
        n += 1
    return out


def _item(e: Evidence) -> dict:
    return {"id": e.entry_id, "source": e.source_name, "valid_from": e.valid("valid_from"), "valid_until": e.valid("valid_until"), "supersedes": e.valid("supersedes")}


def validity_reasons(evidence: list[Evidence], day: date) -> dict[str, str]:
    """{entry id: why it is not current on `day`}: from what each source says about itself (expired, not yet valid, replaced by a source that is in force), and what retrieval already flagged."""
    why = inactive_reasons([_item(e) for e in evidence], day)
    for e in evidence:
        if e.valid("inactive") and e.entry_id not in why:
            why[e.entry_id] = str(e.valid("inactive"))
    return why


def validate(plan: QueryPlan, evidence: list[Evidence], tenant_id: str | None, day: date, sim=None) -> list[EvidenceCheck]:
    """One EvidenceCheck per item. Hard rejections first (tenant, validity), then what the item states: for each requirement of the plan, whether a sentence of it states it."""
    why = validity_reasons(evidence, day)
    common = ev.common_terms([f"{e.heading} {e.content}" for e in evidence])
    reqs = plan.requirements or [plan.question]
    wanted_terms = {t for t in [*plan.attributes, *[w for e in plan.entities for w in ev.terms(e)]] if t and t not in ev.GENERIC}
    checks: list[EvidenceCheck] = []
    for e in evidence:
        c = EvidenceCheck(e.id, "rejected")
        if e.tenant_id is not None and tenant_id is not None and e.tenant_id != tenant_id:
            c.reasons.append("cross_tenant: this evidence belongs to another business")
            checks.append(c)
            continue
        reason = why.get(e.entry_id)
        if reason:
            kind = "superseded" if reason.startswith("superseded") else "not_yet_valid" if reason.startswith("not valid before") else "expired" if reason.startswith("expired") else "not_current"
            c.reasons.append(f"{kind}: {reason}")
            checks.append(c)
            continue
        topic = set(ev.terms(e.heading))
        related, claim_miss = False, False
        for i, req in enumerate(reqs):
            # the retrieval similarity is only ever the SECOND sign the engine's own assessment asks for beside a sentence that is close in meaning; it never supports anything alone
            q_sim = float(e.retrieval_value("similarity", 0.0) or 0.0) if len(reqs) == 1 else 0.0
            g = ev.classify(req, e.content, topic, sim, ev.SEM_MIN, q_sim, common, day, e.source_type == "faq")
            if g["supported"]:
                c.supports[i] = g["sentence"]
            elif g["class"] == "related":
                related = True
                claim_miss = claim_miss or g["reason"].startswith("it asks about")
        if c.supports and plan.scope:  # a statement about another plan, tier or kind of customer is not about this one
            mine = ev.scopes(" ".join(c.supports.values()))
            if mine and "all" not in mine and not (mine & {s.lower() for s in plan.scope}) and not any(s.lower() in e.content.lower() for s in plan.scope):
                c.reasons.append(f"scope_mismatch: it speaks of {', '.join(sorted(mine))}, the question of {', '.join(plan.scope)}")
                c.supports = {}
                related = True
        if c.supports:
            c.status = "accepted"
        elif plan.needs_structured and e.source_type == "row" and (wanted_terms & set(ev.terms(e.heading + " " + e.content)) or e.meta("entity", "").lower() in {x.lower() for x in plan.entities}):
            c.status = "accepted"  # a row of a table is an INPUT to a comparison or a calculation: it is cited for the figures it holds, the operation is done in code
            c.reasons.append("input row for a structured operation")
        elif related:
            c.status = "context"
            c.reasons.append("related_not_the_claim: it is about the same thing, but does not state what was asked" if claim_miss else "related: it does not state what was asked")
        else:
            c.reasons.append("irrelevant: nothing in it bears on the question")
        checks.append(c)
    return checks


def by_id(evidence: list[Evidence]) -> dict[str, Evidence]:
    return {e.id: e for e in evidence}


def sentence_ids(text: str) -> list[str]:
    """Evidence ids written in a text ([E3], E3)."""
    return list(dict.fromkeys(re.findall(r"\bE\d+\b", text or "")))
