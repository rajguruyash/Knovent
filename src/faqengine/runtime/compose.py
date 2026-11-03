"""STAGE 7: ANSWER COMPOSITION, behind one interface (contracts.Composer): compose(plan, evidence, sufficiency, base) -> Composition (a list of Claims, each with the evidence ids it rests on).

  DeterministicComposer   the engine's own answer (answer.respond / response.compose / structured reasoning), as claims. Nothing is generated; sentences are the sources' own, figures are the
                          tables' own or a deterministic calculation. This is the composer of the deterministic mode and the fallback of every other mode.
  MockComposer            a scripted composition, for testing verification and the pipeline's handling of bad answers. It is a test double: its results say nothing about any real model.
  ModelComposer           asks a Provider (contracts.Provider) to compose from the accepted evidence and parses what comes back. It knows no vendor. It is not asked to calculate: a question the
                          structured layer answered keeps that layer's claims. A provider that did not answer is a PROVIDER failure; an answer that cannot be used is a REASONING failure.

Whatever a composer returns is untrusted until claimcheck has verified it. Standard library only.
"""
from __future__ import annotations

import json
import re

try:
    from .contracts import Claim, Composition, Evidence, Provider, ProviderReply, QueryPlan, Sufficiency
except ImportError:  # an exported package puts runtime/ on the path
    from contracts import Claim, Composition, Evidence, Provider, ProviderReply, QueryPlan, Sufficiency

VERDICT = {"faq": "answered", "grounded": "answered", "partial": "partial", "conflict": "conflict", "insufficient": "insufficient", "clarification": "insufficient"}


class DeterministicComposer:
    name = "deterministic"

    def __init__(self, ids: dict[str, str] | None = None):
        self.ids = ids if ids is not None else {}  # entry id -> evidence id

    def compose(self, plan: QueryPlan, evidence: list[Evidence], sufficiency: Sufficiency, base: dict) -> Composition:
        ids = {**{e.entry_id: e.id for e in evidence}, **self.ids}
        by_ref = {s["ref"]: ids.get(s.get("id") or "") for s in base.get("sources", []) or []}
        computed = bool(base.get("computed"))  # worked out by structured reasoning (all of it, or the part of it that was)
        claims: list[Claim] = []
        for c in base.get("claims") or []:
            eids = [by_ref[r] for r in c.get("refs", []) if by_ref.get(r)]
            text = c["text"]
            kind = "qualifier" if text.startswith("Reading:") else "disclosure" if text.startswith(("Not used:", "Not compared", "No value", "I cannot calculate")) else "calc" if computed else "fact"
            claims.append(Claim(text, list(dict.fromkeys(e for e in eids if e)), kind))
        if base.get("outcome") == "conflict":
            for cf in base.get("conflicts") or []:
                for s in cf.get("sources", []):
                    claims.append(Claim(s.get("text", ""), [i for i in [ids.get(s.get("id") or "")] if i], "fact"))
        answer = base.get("answer") or base.get("partial_answer") or base.get("message") or ""
        return Composition(claims, VERDICT.get(str(base.get("outcome")), "insufficient"), answer, self.name, base)


class MockComposer:
    name = "mock"

    def __init__(self, claims: list[Claim] | None = None, verdict: str = "answered", answer: str | None = None, failure: str | None = None, fn=None):
        self.claims, self.verdict, self.answer, self.failure, self.fn = claims or [], verdict, answer, failure, fn

    def compose(self, plan: QueryPlan, evidence: list[Evidence], sufficiency: Sufficiency, base: dict) -> Composition:
        if self.fn:
            return self.fn(plan, evidence, sufficiency, base)
        text = self.answer if self.answer is not None else " ".join(f"{c.text} " + "".join(f"[{i}]" for i in c.evidence_ids) for c in self.claims)
        return Composition(list(self.claims), self.verdict, text, self.name, base, self.failure)


SYSTEM = ("You answer a business question using ONLY the numbered evidence. Evidence ids look like E1, E2; use no other id and no knowledge that is not in the evidence. Respond with ONE JSON object: "
          "verdict (answered | partial | conflict | insufficient), claims (a list of {text, evidence}: each claim is one factual statement and evidence lists the ids that state it), missing (what the "
          "evidence does not establish). Say conflict when two evidence items disagree about the same thing and do not choose between them. A statement about a related thing is not support (an "
          "annual BILLING option is not an annual DISCOUNT; one plan is not every plan). Copy numbers, units and conditions exactly; do not calculate.")
SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string", "enum": ["answered", "partial", "conflict", "insufficient"]}, "missing": {"type": "array", "items": {"type": "string"}},
                                           "claims": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                                                                            "required": ["text", "evidence"]}}}, "required": ["verdict", "claims"]}


def prompt(plan: QueryPlan, evidence: list[Evidence]) -> str:
    ev_text = "\n".join(f"[{e.id}] ({e.source_name} › {e.heading}) {e.content[:900]}" for e in evidence)
    return f"Question: {plan.question}\n\nEvidence:\n{ev_text or '(none)'}"


def parse(text: str):
    t = re.sub(r"<think>.*?</think>", "", re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M), flags=re.S).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        try:
            return json.loads(m.group(0)) if m else None
        except ValueError:
            return None


class ModelComposer:
    name = "model"

    def __init__(self, provider: Provider):
        self.provider = provider
        self.last_reply: ProviderReply | None = None

    def compose(self, plan: QueryPlan, evidence: list[Evidence], sufficiency: Sufficiency, base: dict) -> Composition:
        if base.get("evidence_class") == "computed":  # arithmetic, counting, sorting and filtering are done in code: the model is not asked to restate them
            return DeterministicComposer().compose(plan, evidence, sufficiency, base)
        reply = self.provider.complete(SYSTEM, prompt(plan, evidence), SCHEMA)
        self.last_reply = reply
        return from_reply(reply, plan, base, self.name)


def from_reply(reply: ProviderReply, plan: QueryPlan, base: dict, name: str = "model") -> Composition:
    """A composition from what a provider (or a model on the business's own machine) returned. A provider that did not answer is a PROVIDER failure; an answer that cannot be used is a REASONING failure."""
    if reply.failure:
        return Composition([], "insufficient", "", name, base, reply.failure if reply.failure.startswith("provider:") else f"provider:{reply.failure}")
    data = parse(reply.text)
    if not isinstance(data, dict):
        return Composition([], "insufficient", "", name, base, "reasoning:invalid_json")
    verdict, raw = data.get("verdict"), data.get("claims")
    if verdict not in ("answered", "partial", "conflict", "insufficient") or not isinstance(raw, list):
        return Composition([], "insufficient", "", name, base, "reasoning:schema")
    claims = []
    for c in raw:
        if not isinstance(c, dict) or not isinstance(c.get("text"), str) or not isinstance(c.get("evidence", []), list):
            return Composition([], "insufficient", "", name, base, "reasoning:malformed_claim")
        claims.append(Claim(c["text"].strip(), [str(i) for i in c.get("evidence", []) if isinstance(i, (str, int))], "fact"))
    text = " ".join(c.text + " " + "".join(f"[{i}]" for i in c.evidence_ids) for c in claims)
    return Composition(claims, verdict, text.strip(), name, base)
