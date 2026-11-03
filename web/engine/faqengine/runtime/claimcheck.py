"""STAGE 8: CLAIM-LEVEL VERIFICATION. The answer is a list of claims, each with the evidence ids it rests on. Each is checked against ONLY the evidence it cites, so a citation that does not support
its claim fails, whatever else is in the knowledge:

  * every id is one the request made and validation accepted (no invented id, no other business's, no evidence that was only context);
  * the cited evidence states the claim (verify.verify: wording, figures, dates, polarity, commitments, and the conditions the cited sentence carries);
  * a claim of a KIND (a discount, a refund, a trial, a guarantee, an exception) is stated by its evidence as that kind (annual billing is not an annual discount);
  * the scope is not widened ("all plans", "every customer", "always" where the evidence names one plan, one tier or one condition);
  * a calculated claim uses only numbers that are in its cited rows or in the deterministic result.

`resolve` then acts on a failure: the claim is removed, the answer is rebuilt from the claims that held, and if none did the answer is an abstention. An answer that lost a claim is never reported as
verified. Qualifiers and disclosures (a reading, what was left out) state no business fact; they may add no number. Standard library only; no model.
"""
from __future__ import annotations

import re

try:
    from . import evidence as ev
    from . import verify as V
    from .contracts import Claim, ClaimReport, Composition, Evidence
except ImportError:  # an exported package puts runtime/ on the path
    import evidence as ev
    import verify as V
    from contracts import Claim, ClaimReport, Composition, Evidence

CITES = re.compile(r"\s*\[(?:E?\d+(?:\s*,\s*E?\d+)*)\]")
UNIVERSAL_CLAIM = re.compile(r"\b(?:all|every|each|any)\s+(?:\w+\s+)?(?:plans?|customers?|users?|deployments?|tiers?|accounts?|editions?|products?|regions?)\b|\b(?:always|never|everywhere|everyone|in every case)\b", re.I)
CLAIM_KINDS = ("discount", "refund", "trial", "guarantee")  # kinds whose wording is the same in a claim as in a question; 'exception' and 'condition' read a question, not a statement
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


UNIT = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(usd|eur|gbp|inr|mb|gb|tb|kb|%|minutes?|hours?|days?|weeks?|months?|years?|seats?|agents?|users?)\b", re.I)


def _pairs(text: str) -> set[tuple[str, str]]:
    return {(n.replace(",", ""), u.lower().rstrip("s")) for n, u in UNIT.findall(CITES.sub(" ", text or ""))}


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in NUM.findall(CITES.sub(" ", text or ""))}


def check(claims: list[Claim], evidence: dict[str, Evidence], accepted: set[str], tenant_id: str | None = None, computed: set[str] | None = None, question: str = "", sim=None) -> ClaimReport:
    out, issues, removed = [], [], 0
    pool_numbers = set().union(*[_numbers(e.content) for e in evidence.values()]) if evidence else set()
    computed = {c.replace(",", "") for c in (computed or set())}
    for c in claims:
        found: list[dict] = []

        def flag(kind: str, detail: str, _c=c, _f=found) -> None:
            _f.append({"type": kind, "claim": _c.text[:160], "detail": detail, "evidence_ids": list(_c.evidence_ids)})

        cited = [evidence[i] for i in c.evidence_ids if i in evidence]
        if c.kind in ("qualifier", "disclosure"):  # states no business fact: it may not bring a number of its own
            extra = _numbers(c.text) - pool_numbers - computed
            if extra:
                flag("added_number", f"{sorted(extra)} is in no evidence and in no calculation")
            out.append({"text": c.text, "evidence_ids": c.evidence_ids, "kind": c.kind, "status": "removed" if found else "not_factual", "issues": [f["type"] for f in found]})
            issues += found
            removed += bool(found)
            continue
        if not c.evidence_ids:
            flag("no_citation", "a factual claim has no evidence")
        for i in c.evidence_ids:
            if i not in evidence:
                flag("fabricated_evidence_id", f"{i} is not an evidence id of this request")
            elif tenant_id is not None and evidence[i].tenant_id not in (None, tenant_id):
                flag("cross_tenant", f"{i} belongs to another business")
            elif i not in accepted:
                flag("citation_not_accepted", f"{i} was not accepted as evidence for this question")
        if cited and not found:
            body = CITES.sub(" ", c.text).strip()
            if c.kind == "calc":
                have_pairs = set().union(*[_pairs(e.content) for e in cited]) | _pairs(question)
                have_numbers = set().union(*[_numbers(e.content) for e in cited]) | _numbers(question) | computed
                sentences = [x for e in cited for x in ev.sentences(e.content)]
                with_unit = _pairs(body)

                def unit_at(n: str, u: str, sentences=sentences) -> bool:  # a table keeps its unit in the header ("Max file upload (MB) limit, growth: 100."): the number and the unit are in one sentence
                    return any(n in _numbers(x) and re.search(rf"\b{re.escape(u)}", x, re.I) for x in sentences)

                extra = {n for n, u in with_unit if (n, u) not in have_pairs and n not in computed and not unit_at(n, u)}  # a figure with a unit must be that figure, with that unit, in the cited rows
                extra |= {n for n in _numbers(body) - {n for n, _ in with_unit} - have_numbers}
                if extra:
                    flag("changed_number", f"{sorted(extra)} is neither in the cited rows nor a result of the calculation")
            else:
                v = V.verify(body, [{"ref": n, "text": e.content} for n, e in enumerate(cited, 1)], "grounded", question, sim)
                for i in v["issues"]:
                    flag(i["type"], i["detail"])
                for kind in CLAIM_KINDS:  # a claim of a kind needs evidence that states that kind
                    q, a = ev.CLAIM_RE[kind]
                    if q.search(body) and not any(a.search(e.content) for e in cited):
                        flag("different_claim", f"it states a {kind}; the cited evidence does not")
            if UNIVERSAL_CLAIM.search(body) and not any(UNIVERSAL_CLAIM.search(e.content) or ev.UNIVERSAL.search(e.content) for e in cited):
                flag("scope_expanded", "it speaks of all of them (or always); the cited evidence does not")
            mine = ev.scopes(body)
            if mine and "all" not in mine:
                theirs = set().union(*[ev.scopes(e.content) | {w.lower() for w in re.findall(r"[A-Za-z][\w-]*", e.heading + " " + (e.meta("entity", "") or ""))} for e in cited])
                if not (mine & theirs) and not any(m in " ".join(e.content.lower() for e in cited) for m in mine):
                    flag("scope_mismatch", f"it is about {', '.join(sorted(mine))}; the cited evidence is not")
        out.append({"text": c.text, "evidence_ids": c.evidence_ids, "kind": c.kind, "status": "removed" if found else "verified", "issues": sorted({f["type"] for f in found})})
        issues += found
        removed += bool(found)
    return ClaimReport(passed=removed == 0, claims=out, removed=removed, issues=issues)


def resolve(comp: Composition, report: ClaimReport, evidence: dict[str, Evidence]) -> Composition:
    """The composition after verification. Nothing removed: it is returned as it was. Something removed: the answer is rebuilt from what held (never silently), marked partial, and says that a
    statement was left out; nothing held: an abstention."""
    if report.passed:
        return comp
    keep = [c for c, r in zip(comp.claims, report.claims, strict=True) if r["status"] != "removed"]
    facts = [c for c in keep if c.kind in ("fact", "calc")]
    if not facts:
        return Composition([], "insufficient", "", comp.composer, comp.base, comp.failure)
    ids = list(dict.fromkeys(i for c in keep for i in c.evidence_ids if i in evidence))
    number = {i: n for n, i in enumerate(ids, 1)}
    text = " ".join(CITES.sub("", c.text).strip() + (" " + "".join(f"[{number[i]}]" for i in c.evidence_ids if i in number) if c.evidence_ids else "") for c in keep)
    note = Claim(f"{report.removed} statement{'s' if report.removed != 1 else ''} could not be verified against the evidence and {'were' if report.removed != 1 else 'was'} left out.", [], "disclosure")
    return Composition(keep + [note], "partial", text + " " + note.text, comp.composer, comp.base, comp.failure)
