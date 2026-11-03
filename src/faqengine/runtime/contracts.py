"""The contracts between the stages of the reasoning pipeline (pipeline.py). Plain data and small interfaces, nothing else, so that every stage can be tested alone and any provider can be put
behind the model interface later:

    question -> QueryPlan (understand.py) -> [hybrid retrieval, unchanged] -> Evidence (evidence_check.normalize) -> EvidenceCheck (evidence_check.validate)
             -> Sufficiency (sufficiency.assess) -> [one targeted retrieval, targeted.py] -> Composition (compose.py) -> ClaimReport (claimcheck.py) -> final answer + ExecutionMeta

Two rules hold across all of them. An evidence id (E1, E2, ...) is made once, before any model sees anything, and never changes; a model may refer to an id and never creates or alters one. And a
failure of a PROVIDER (it did not answer) is kept apart from a failure of the REASONING (it answered and the answer was not good enough).
Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# ---------- the question

@dataclass
class Condition:
    text: str  # what must hold, in the customer's words ("WhatsApp", "more than 5 agents", "annual discount")
    terms: list[str] = field(default_factory=list)
    kind: str = "feature"  # feature | threshold | attribute | claim
    claim_type: str | None = None  # discount, refund, trial, guarantee, eligibility ... (evidence.CLAIMS): a statement of another kind does not satisfy it
    qualifier: str | None = None  # annual, monthly, student ...: part of what is asked, never dropped


@dataclass
class QueryPlan:
    question: str
    intent: str = "lookup"  # lookup | filter | superlative | comparison | difference | count | calculation | limit_check | yes_no | policy | open
    entities: list[str] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    conditions: list[Condition] = field(default_factory=list)
    comparison: dict | None = None  # {"operation": "min" | "max" | "difference" | "sum" | "count" | "average", "attribute": ...}
    scope: list[str] = field(default_factory=list)  # who or what it is about: plan names, kinds of customer
    qualifiers: list[str] = field(default_factory=list)  # both, every, only, always, never, at least ...
    temporal: dict = field(default_factory=dict)  # {"day": ISO date or None, "words": [...]}
    output_format: str = "prose"  # prose | yes_no | list | number | comparison
    needs_structured: bool = False
    multi_source: bool = False
    parts: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)  # one per thing the answer must establish
    interpreted_by: str = "deterministic"


# ---------- the evidence

@dataclass(frozen=True)
class Evidence:
    id: str  # E1, E2 ...: made before any inference, never changed
    tenant_id: str | None
    entry_id: str  # the chunk, row or FAQ entry in the knowledge base
    source_id: str  # the source it was read from (its name or address)
    source_type: str  # faq | passage | row
    source_name: str
    location: str  # the file or address, and the section
    heading: str
    content: str
    metadata: tuple = ()  # (key, value) pairs: category, table, version ...
    validity: tuple = ()  # (key, value) pairs: valid_from, valid_until, supersedes, inactive
    retrieval: tuple = ()  # (key, value) pairs: method, score, rank, found_by (a score says where it was found, never that it is true)

    def meta(self, key: str, default=None):
        return dict(self.metadata).get(key, default)

    def valid(self, key: str, default=None):
        return dict(self.validity).get(key, default)

    def retrieval_value(self, key: str, default=None):
        return dict(self.retrieval).get(key, default)


@dataclass
class EvidenceCheck:
    evidence_id: str
    status: str  # accepted | context | rejected
    reasons: list[str] = field(default_factory=list)  # why, in words a person can read
    supports: dict = field(default_factory=dict)  # requirement index -> the sentence that supports it


@dataclass
class Sufficiency:
    status: str  # SUFFICIENT | INSUFFICIENT | PARTIAL | CONFLICTED | AMBIGUOUS
    supported: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    unverified_conditions: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    needs_retrieval: bool = False
    gap_queries: list[str] = field(default_factory=list)
    action: str = "answer"  # answer | qualify | refuse
    notes: list[str] = field(default_factory=list)


# ---------- the answer

@dataclass
class Claim:
    text: str
    evidence_ids: list[str] = field(default_factory=list)
    kind: str = "fact"  # fact | calc (worked out by structured reasoning) | qualifier (a reading or assumption, said as such) | disclosure (what was left out)


@dataclass
class Composition:
    claims: list[Claim] = field(default_factory=list)
    verdict: str = "answered"  # answered | partial | conflict | insufficient
    answer: str = ""
    composer: str = "deterministic"
    base: dict = field(default_factory=dict)  # the engine's own result this was made from (deterministic composer): outcome, sources, conflicts ...
    failure: str | None = None  # provider:<kind> or reasoning:<kind>


@dataclass
class ClaimReport:
    passed: bool
    claims: list[dict] = field(default_factory=list)  # {text, evidence_ids, kind, status: verified | removed | not_factual, issues: [...]}
    removed: int = 0
    issues: list[dict] = field(default_factory=list)


@dataclass
class ExecutionMeta:
    mode: str = "deterministic"  # deterministic | platform | byok | local
    provider: str | None = None
    model: str | None = None
    inference_attempted: bool = False
    inference_succeeded: bool = False
    fallback: bool = False
    generated_by: str = "deterministic"  # deterministic | model | deterministic_fallback | deterministic_no_consent
    failure_kind: str | None = None  # provider:<kind> | reasoning:<kind> | consent:<why>
    validation: str = ""  # what the verification concluded: verified | partly_verified | not_verified
    retrieval_passes: int = 1
    stage_ms: dict = field(default_factory=dict)
    tokens_in: int = 0  # what the provider reported for this request's call (0 when no call was made or it reported nothing)
    tokens_out: int = 0
    cost: float | None = None  # estimated from a configured or verified price; None when there is none
    provider_ms: float | None = None
    provider_attempts: int = 0  # requests made to the one provider and model, retries included
    answer_path: str = ""  # fast_path | model | deterministic | deterministic_fallback | deterministic_no_consent | abstained | unavailable | mock: how THIS answer was made
    fast_path_reason: str = ""  # why the model was skipped (exact_faq_direct, structured_computed), or why the fast path was not taken (declined:..., not_applicable:..., disabled)
    fast_path_taken: bool = False


# ---------- what a provider, an interpreter and a consent record look like (nothing here knows any vendor)

@dataclass
class ProviderReply:
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    failure: str | None = None  # "provider:<kind>": rate_limited, server, timeout, network, auth, not_found, quota, bad_request, blocked, cancelled, circuit_open
    cost: float | None = None  # estimated, from a price that someone configured or verified; None when there is no price (never guessed)
    attempts: int = 1  # how many requests it took (bounded retries of the SAME provider and model: never another one)
    model: str | None = None  # what answered, as the adapter was configured


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, user: str, schema: dict | None = None) -> ProviderReply: ...


class Interpreter(Protocol):
    """Optional semantic help for query understanding: returns corrections to a deterministic QueryPlan as a dict of fields, or None. Whatever it returns is validated; it can add conditions and
    entities, it can never remove one the deterministic reading found."""

    def interpret(self, question: str, plan: QueryPlan) -> dict | None: ...


class ConsentPolicy(Protocol):
    def allows(self, tenant_id: str | None, provider_name: str) -> bool: ...


class Composer(Protocol):
    name: str

    def compose(self, plan: QueryPlan, evidence: list[Evidence], sufficiency: Sufficiency, base: dict) -> Composition: ...
