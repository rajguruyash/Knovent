"""STAGE 1: QUERY UNDERSTANDING. The question as a structured QueryPlan: what is asked, about what, under which conditions and scope, which operation it needs, and what the answer must establish
(`requirements`). Deterministic and local: nothing here calls a model. An optional Interpreter (contracts.Interpreter) can add to a plan later; it can never take away what this reading found.

What it is careful about is what a looser normalisation would lose:
  * every condition of a conjunction is kept ("support both WhatsApp and SSO" is two conditions, and `both` is kept as a qualifier);
  * a kind of claim is kept apart from a word near it ("Is the annual plan discounted?" asks for a DISCOUNT, qualified by ANNUAL: a statement that annual billing is available is not that);
  * a comparison is a comparison ("Which plan is cheapest?" is a minimum over a price, not a lookup of whatever sentence mentions plans and prices).
"""
from __future__ import annotations

import re
from datetime import date

try:
    from . import evidence as ev
    from . import structured as S
    from . import temporal
    from .contracts import Condition, QueryPlan
except ImportError:  # an exported package puts runtime/ on the path
    import evidence as ev
    import structured as S
    import temporal
    from contracts import Condition, QueryPlan

QUALIFIER_RE = re.compile(r"\b(both|all of|every|each|only|always|never|at least|at most|no more than|more than|fewer than|less than|up to|unlimited|either|neither|except|excluding|without|any)\b", re.I)
PERIOD_RE = re.compile(r"\b(annual|annually|yearly|monthly|weekly|quarterly|student|students|non-?profit|new customers?|existing customers?|enterprise|business|free|paid|trial)\b", re.I)
FEATURE_VERB = re.compile(r"\b(?:include|includes|included|have|has|support|supports|supported|offer|offers|provide|provides|allow|allows|come with|comes with|contain|contains|cover|covers|get|gets|use|uses|with)\b\s+(?P<rest>.+)$", re.I)
THRESHOLD = re.compile(r"\b(?:at least|no fewer than|more than|over|above|at most|up to|no more than|fewer than|less than|under|below)\s+\d[\d,.]*", re.I)
TIME_WORDS = re.compile(r"\b(currently|now|today|at the moment|as of|from|until|till|before|after|since|next|last|this (?:year|month)|by)\b", re.I)
LEAD = re.compile(r"^\s*(?:which|what|who|how many|how much|list|show|name|are there any|is there any|is there an?|do|does|can|could|will|is|are)\b[^,]*?\b(?:plans?|tiers?|editions?|products?|options?|integrations?|offices?)?\b", re.I)


def _entities(question: str, known: set[str]) -> list[str]:
    qw = S.words(question)
    found: list[str] = []
    for name in sorted(known, key=lambda n: -len(n)):
        nw = S.words(name)
        if nw and any(qw[i:i + len(nw)] == nw for i in range(len(qw))) and not any(set(nw) <= set(S.words(x)) for x in found):
            found.append(name)
    for m in re.finditer(r"\b[A-Z][\w-]*(?:\s+[A-Z][\w-]*)*", question):
        name = m.group(0)
        if m.start() == 0 or name.lower() in ev.QUESTION_WORD_SET or name.lower() in ev.NOT_NAMES or name == "I":
            continue
        if name.lower() not in {x.lower() for x in found} and not any(name.lower() in x.lower() for x in found):
            found.append(name)
    return found


def _conditions(part: str, entities: list[str]) -> list[Condition]:
    out: list[Condition] = []
    qualifier = next((m.group(1).lower() for m in PERIOD_RE.finditer(part)), None)
    for claim_type, (q, _) in ev.CLAIM_RE.items():  # a kind of claim: a statement of another kind does not satisfy it
        if q.search(part):
            text = f"{qualifier} {claim_type}" if qualifier else claim_type
            out.append(Condition(text, ev.terms(text), "claim", claim_type, qualifier))
    part = temporal.DATE.sub(" ", part)  # a date is a temporal constraint, not a condition
    m = FEATURE_VERB.search(part) if not re.match(r"\s*(when|where|why|how long|who|how soon)\b", part, re.I) else None
    if m and not out:
        rest = re.sub(r"^\s*(?:both|all of|either)\s+", "", m.group("rest").strip(" ?."), flags=re.I)
        pieces = [p.strip() for p in re.split(r"\s*(?:,|&|\band\b|\bas well as\b)\s*", rest) if p.strip()]
        for p in pieces:
            terms = [t for t in S.content(p) if t not in S.WH]
            if not terms:
                continue
            kind = "threshold" if (THRESHOLD.search(p) or re.search(r"\d", p)) else "feature"
            out.append(Condition(p, terms, kind))
    t = THRESHOLD.search(part)
    if not out and t and not re.match(r"\s*(when|where|why|how long)\b", part, re.I):
        out.append(Condition(t.group(0), S.content(part), "threshold"))
    return out


def _intent(q: str, plan_entities: list[str], conditions: list[Condition]) -> tuple[str, dict | None]:
    mn, mx = bool(S.MIN_RE.search(q)), bool(S.MAX_RE.search(q))
    if S.DIFF_RE.search(q) and len(plan_entities) >= 2:
        return "difference", {"operation": "difference", "between": plan_entities[:2]}
    if re.search(r"\b(\d[\d,.]*)\s+\w+", q) and re.search(r"\b(cost|price|pay|total|charge|how much)\b", q, re.I) and not S.SUM_RE.search(q):
        return "calculation", {"operation": "multiply"}
    if S.CAN_RE.search(q.strip()) and re.search(r"\b\d[\d,.]*\s+[A-Za-z]", q):
        return "limit_check", {"operation": "compare_to_limit"}
    if mn != mx:
        return "superlative", {"operation": "min" if mn else "max"}
    if S.SUM_RE.search(q):
        return "count" if S.COUNT_RE.search(q) else "calculation", {"operation": "average" if re.search(r"average|mean", q, re.I) else "sum"}
    if S.COUNT_RE.search(q):
        return "count", {"operation": "count"}
    if THRESHOLD.search(q) and re.search(r"\b(which|what|list)\b", q, re.I):
        return "filter", {"operation": "threshold"}
    if S.FILTER_RE.search(q) and conditions:
        return "filter", {"operation": "intersect" if len(conditions) > 1 else "filter"}
    if re.match(r"\s*(?:do|does|did|can|could|is|are|will|would|may|should)\b", q, re.I):
        return "yes_no", None
    if re.search(r"\b(polic(y|ies)|process|procedure|rules?|terms|conditions?|requirements?|how does|what happens|explain|describe)\b", q, re.I):
        return "policy", None
    return "lookup", None


def understand(question: str, known_entities: set[str] | frozenset[str] = frozenset(), interpreter=None, today: date | None = None) -> QueryPlan:
    q = re.sub(r"\s+", " ", question or "").strip()
    parts = ev.split_parts(q) if q else []
    entities = _entities(q, {e.lower() for e in known_entities})
    conditions: list[Condition] = []
    by_part: dict[str, list[Condition]] = {}
    for p in parts:
        for c in _conditions(p, entities):
            by_part.setdefault(p, []).append(c)
            if c.text.lower() not in {x.text.lower() for x in conditions}:
                conditions.append(c)
    intent, comparison = _intent(q, [e for e in entities if e.lower() in {k.lower() for k in known_entities}] or entities, conditions)
    qualifiers = sorted({m.group(1).lower() for m in QUALIFIER_RE.finditer(q)})
    days = temporal.dates_in(q)
    plan = QueryPlan(
        question=q, intent=intent, entities=entities,
        attributes=[t for t in ev.terms(q) if t not in {x.lower() for e in entities for x in ev.terms(e)} and t not in ev.GENERIC and t not in S.WH],
        conditions=conditions, comparison=comparison, scope=sorted(ev.scopes(q) | {e.lower() for e in entities if re.search(rf"\b{re.escape(e)}\s+(plans?|tiers?|customers?)\b", q, re.I)}),
        qualifiers=qualifiers,
        temporal={"day": (max(days).isoformat() if days else None), "words": sorted({m.group(1).lower() for m in TIME_WORDS.finditer(q)}), "as_of": (today or date.today()).isoformat()},
        output_format=("yes_no" if intent == "yes_no" else "comparison" if intent in ("difference",) else "number" if intent in ("count", "calculation", "limit_check") or re.match(r"\s*how (many|much)\b", q, re.I)
                       else "list" if intent == "filter" or re.match(r"\s*(which|list|show|name)\b", q, re.I) else "prose"),
        needs_structured=intent in ("difference", "calculation", "limit_check", "superlative", "count", "filter"),
        multi_source=len(parts) > 1 or len(conditions) > 1 or intent in ("difference", "filter"),
        parts=parts,
    )
    reqs: list[str] = []
    for p in parts or [q]:  # what each part must establish: its conditions when it has several (or is a yes/no or a filter), else the part itself
        cs = by_part.get(p, [])
        reqs += [c.text for c in cs] if (len(cs) > 1 or (cs and intent in ("filter", "yes_no"))) else [p]
    plan.requirements = list(dict.fromkeys(reqs))
    if interpreter is not None:
        plan = merge(plan, interpreter.interpret(q, plan))
    return plan


def merge(plan: QueryPlan, extra: dict | None) -> QueryPlan:
    """What an interpreter (a model, later) may add to a reading: entities, attributes, conditions, qualifiers, scope. It cannot remove anything the deterministic reading found, and what it
    returns is checked for type first; anything malformed is ignored."""
    if not isinstance(extra, dict):
        return plan
    for key in ("entities", "attributes", "qualifiers", "scope"):
        add = extra.get(key)
        if isinstance(add, list):
            have = {x.lower() for x in getattr(plan, key)}
            getattr(plan, key).extend(str(x)[:80] for x in add if isinstance(x, str) and x.strip() and x.lower() not in have)
    for c in extra.get("conditions") or []:
        if isinstance(c, str) and c.strip() and c.lower() not in {x.text.lower() for x in plan.conditions}:
            plan.conditions.append(Condition(c.strip()[:120], S.content(c), "feature"))
            plan.requirements.append(c.strip()[:120])
    plan.interpreted_by = "deterministic+interpreter"
    return plan
