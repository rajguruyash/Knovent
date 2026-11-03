"""STAGE 5: THE SUFFICIENCY ASSESSMENT. Given the plan and what validation accepted, can the question be answered, in part, or not, and what is missing?

    SUFFICIENT    every requirement is established by accepted evidence (or by a structured operation over accepted rows)
    PARTIAL       some are, some are not: answer what is established and name what is not
    CONFLICTED    accepted sources disagree about the same thing: both are disclosed, none is chosen
    INSUFFICIENT  nothing established
    AMBIGUOUS     the question cannot be answered without being narrowed (it names nothing, or fits several different things equally)

It does not abstain because the wording differs (support is judged by what a sentence states, evidence.classify, not by exact words) and it does not answer because a similar passage exists
(a passage that only comes near is `context`, never support). When a gap is one a second search could close (a named entity, condition or attribute no accepted evidence mentions), it says so
and proposes the query: targeted.py runs it at most once. Standard library only; no model.
"""
from __future__ import annotations

try:
    from . import evidence as ev
    from .contracts import Evidence, EvidenceCheck, QueryPlan, Sufficiency
except ImportError:  # an exported package puts runtime/ on the path
    import evidence as ev
    from contracts import Evidence, EvidenceCheck, QueryPlan, Sufficiency


def assess(plan: QueryPlan, evidence: list[Evidence], checks: list[EvidenceCheck], conflicts: list[dict] | None = None, structured: list[dict] | None = None,
           clarification: dict | None = None, second_pass_done: bool = False) -> Sufficiency:
    reqs = plan.requirements or [plan.question]
    accepted = {c.evidence_id for c in checks if c.status == "accepted"}
    by_req: dict[int, list[str]] = {i: [] for i in range(len(reqs))}
    for c in checks:
        if c.status == "accepted":
            for i in c.supports:
                by_req[i].append(c.evidence_id)
    computed = [s for s in (structured or []) if s and s.get("lines")]  # a structured operation that produced a result establishes what it was asked
    supported = [r for i, r in enumerate(reqs) if by_req[i] or (computed and plan.needs_structured)]
    unsupported = [r for r in reqs if r not in supported]
    conds = [c.text for c in plan.conditions]
    unverified = [c for c in conds if c in unsupported]
    s = Sufficiency("INSUFFICIENT", supported, unsupported, unverified, list(conflicts or []))
    if conflicts:
        s.status, s.action = "CONFLICTED", "qualify"
        s.notes.append("accepted sources disagree about the same thing: both are shown, neither is chosen")
    elif clarification and not supported:
        s.status, s.action = "AMBIGUOUS", "refuse"
        s.notes.append(clarification.get("reason", "the question needs narrowing"))
    elif not unsupported and (supported or accepted):
        s.status, s.action = "SUFFICIENT", "answer"
    elif supported:
        s.status, s.action = "PARTIAL", "qualify"
        s.notes.append("only part of the question is established: " + "; ".join(unsupported) + " is not")
    else:
        s.status, s.action = "INSUFFICIENT", "refuse"
        if any(r.startswith(("expired", "superseded", "not_yet_valid")) for c in checks for r in c.reasons):
            s.notes.append("what was found is out of date")
    if s.status == "SUFFICIENT" and plan.intent == "filter" and len(plan.conditions) > 1 and not computed:  # each condition holds somewhere does not mean they hold TOGETHER for one thing
        s.status, s.action = "PARTIAL", "qualify"
        s.unverified_conditions = ["all of: " + " and ".join(conds)]
        s.notes.append("each condition is stated separately; that one thing has all of them is not established")
    if unsupported and not second_pass_done and s.status in ("PARTIAL", "INSUFFICIENT"):  # a gap a second search might close
        for r in unsupported:
            terms = [t for t in ev.terms(r) if t not in ev.GENERIC]
            if not terms:
                continue
            q = " ".join(dict.fromkeys([r, *plan.entities, *plan.scope]))
            if q.strip().lower() != plan.question.strip().lower() and q not in s.gap_queries:
                s.gap_queries.append(q)
        s.needs_retrieval = bool(s.gap_queries)
    return s
