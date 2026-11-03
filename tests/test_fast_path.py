"""THE DETERMINISTIC FAST PATH (Pipeline.evaluate_fast_path), on the real components and the real example knowledge of two fictional businesses (Acme: FAQ, documents, sheets, JSON, web pages, expired and
replaced material; Beacon: tables, calculations, dates). A provider that COUNTS its calls stands in for the model, so "the model was not asked" is observed, not assumed.

What is proved: an exact FAQ answer and a fully supported structured answer are given without a model, with the same text and citations the deterministic engine gives; everything else (a meaning
match, a passage, a partial answer, a conflict, a missing part, old or foreign evidence, a failed verification) continues through the pipeline exactly as before; a similarity score is never the
reason; missing information stays an abstention; and every answer records how it was made. Development and regression questions only: the sealed sets are never used here."""
import dataclasses
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import unified_benchmark as UB  # noqa: E402

from faqengine.runtime import claimcheck  # noqa: E402
from faqengine.runtime.contracts import ProviderReply  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402
from faqengine.runtime.pipeline import Pipeline  # noqa: E402

MODEL_DIR = ROOT / "hosted_models" / "minilm-l6" / "model"
pytestmark = pytest.mark.skipif(not MODEL_DIR.is_dir(), reason="needs the base embedding model (hosted_models/minilm-l6)")
DAY = date.fromisoformat(UB.AS_OF)


class Counting:
    """A model stand-in that counts its calls and, when asked, restates the first numbered evidence item with its id (what a well-behaved model does)."""
    name, model = "counting", "m-1"

    def __init__(self):
        self.calls: list[str] = []

    def complete(self, system, user, schema=None):
        self.calls.append(user)
        m = re.search(r"\[(E\d+)\] \([^)]*\) (.*)", user)
        if not m:
            return ProviderReply(text=json.dumps({"verdict": "insufficient", "claims": [], "missing": ["everything"]}), tokens_in=10, tokens_out=5, seconds=0.01)
        first = re.split(r"(?<=[.!?])\s", m.group(2).strip())[0]
        return ProviderReply(text=json.dumps({"verdict": "answered", "claims": [{"text": first, "evidence": [m.group(1)]}], "missing": []}), tokens_in=40, tokens_out=20, seconds=0.01)


class AllowAll:
    def allows(self, tenant_id, provider_name):
        return True


def build(data):
    UB.DATA = (ROOT / data).resolve()
    emb = Embedder(MODEL_DIR)
    kb, index, _ = UB.build(emb)

    def search(q, on=None):
        f, p, d = index.search(q, emb.embed([q], "query")[0], **({"on": on} if on is not None else {}))
        d["embed_ms"] = d["total_ms"] = 0.0
        return f, p, d

    search.model = index.fact_model
    return search, lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []


@pytest.fixture(scope="module")
def acme():
    return build("examples/unified")


@pytest.fixture(scope="module")
def beacon():
    return build("examples/reasoning")


def run(world, q, fast=True, provider=None, mode="byok", tenant="t-1"):
    search, sim = world
    p = provider or Counting()
    return Pipeline(mode, p, AllowAll(), tenant_id=tenant, fast_path=fast).run(q, search, sim, as_of=DAY), p


def state(world, q, fast=True, tenant="t-1"):
    search, sim = world
    pipe = Pipeline("byok", Counting(), AllowAll(), tenant_id=tenant, fast_path=fast)
    return pipe, pipe.prepare(q, search, sim, as_of=DAY)


# ================================================================== what is skipped, and what is recorded
def test_an_exact_faq_answer_is_given_without_a_model_and_is_the_deterministic_engines_own_answer(acme):
    r, provider = run(acme, "Do you offer a free trial?")
    ex = r["execution"]
    assert provider.calls == []  # observed: the model was never asked
    assert (ex["answer_path"], ex["fast_path_reason"], ex["model_invoked"], ex["provider_steps_tried"]) == ("fast_path", "exact_faq_direct", False, [])
    assert ex["generated_by"] == "deterministic" and ex["inference_attempted"] is False and r["verified"] is True and ex["validation"] == "verified"
    assert ex["evidence_ids"] and ex["evidence_ids"][0].startswith("E") and ex["verification"] == {"outcome": "verified", "passed": True, "claims_checked": len(r["claims"]), "claims_removed": 0}
    assert isinstance(ex["latency_ms"], float) and ex["latency_ms"] >= 0
    baseline, _ = run(acme, "Do you offer a free trial?", mode="deterministic")
    assert r["answer"] == baseline["answer"] and [s["source"] for s in r["sources"]] == [s["source"] for s in baseline["sources"]] and r["outcome"] == "faq"  # same words, same citations


def test_every_claim_of_a_fast_answer_cites_evidence_that_was_accepted_and_the_citation_verifies(acme):
    r, _ = run(acme, "How do I reset my password?")
    assert r["execution"]["answer_path"] == "fast_path"
    accepted = {e["id"] for e in r["evidence"] if e["status"] == "accepted"}
    assert r["claims"] and all(c["evidence_ids"] and set(c["evidence_ids"]) <= accepted for c in r["claims"])
    assert r["claim_verification"]["passed"] is True and r["claim_verification"]["removed"] == 0


def test_a_calculation_or_lookup_worked_out_in_code_takes_the_fast_path_and_the_model_is_not_asked(beacon):
    r, provider = run(beacon, "How much would 12 extra agents cost on the Growth plan?")
    ex = r["execution"]
    assert provider.calls == [] and "72" in (r["answer"] or "") and (ex["answer_path"], ex["fast_path_reason"], ex["model_invoked"]) == ("fast_path", "structured_computed", False)
    assert any(c["kind"] == "calc" for c in r["claims"]) and r["verified"] is True
    r2, p2 = run(beacon, "What would 10 Growth plans cost?")
    assert p2.calls == [] and r2["execution"]["answer_path"] == "fast_path" and r2["outcome"] == "grounded"


def test_a_comparison_over_a_whole_table_is_worked_out_in_code_and_declined_by_the_stricter_validation_check(beacon):
    r, provider = run(beacon, "Which plan is the cheapest?")  # every row is looked at; validation accepted some rows only as context, and the fast path needs the cited evidence to be ACCEPTED
    ex = r["execution"]
    assert ex["fast_path_reason"] == "declined:evidence_not_accepted" and ex["answer_path"] == "deterministic" and ex["model_invoked"] is False and provider.calls == []  # as before: the code computes it, no model restates it


def test_a_structured_answer_the_independent_check_does_not_confirm_is_still_worked_out_in_code_but_is_not_called_the_fast_path(beacon):
    r, provider = run(beacon, "How much does the Enterprise plan cost per month?")  # the table says "Contact us": there is no number to calculate
    ex = r["execution"]
    assert ex["fast_path_reason"].startswith("declined:sufficiency_not_confirmed") and ex["answer_path"] == "deterministic"
    assert provider.calls == [] and ex["model_invoked"] is False  # as before: a model never restates what the code computed


# ================================================================== what is not skipped
def test_a_match_by_meaning_is_not_an_exact_match_so_the_pipeline_continues_and_the_model_composes(acme):
    r, provider = run(acme, "Where do you keep customer data?")  # the FAQ says "Where is my data stored?": the engine rates it `semantic`, not `direct`
    ex = r["execution"]
    assert ex["fast_path_reason"] == "not_eligible:faq/semantic" and len(provider.calls) == 1 and ex["answer_path"] == "model" and ex["model_invoked"] is True and ex["provider_steps_tried"] == ["byok"]
    assert r["verified"] is True  # (the model's claims were verified like any other)


def test_the_faqs_own_alternative_phrasing_is_an_exact_match_but_a_loose_paraphrase_that_finds_nothing_stays_an_abstention(acme):
    r, provider = run(acme, "Is there a trial period?")  # listed as an alternative question of the free-trial entry
    assert r["execution"]["answer_path"] == "fast_path" and provider.calls == []
    r2, p2 = run(acme, "Is it possible to try the product free of charge before buying?")  # no entry says this in these words and the engine does not find the trial entry
    assert r2["outcome"] == "insufficient" and r2["answer"] is None and r2["execution"]["answer_path"] != "fast_path"  # not forced into an answer


def test_conflicting_sources_are_never_fast_pathed_and_the_pipeline_continues_as_before(acme):
    pipe, st = state(acme, "When are invoices due?")  # the FAQ says 30 days, the billing terms say 15
    d = pipe.evaluate_fast_path(st)
    assert st.base["outcome"] == "conflict" and d.taken is False and d.reason == "not_eligible:conflict/contradiction"
    assert st.base["conflicts"] and {s["source"] for c in st.base["conflicts"] for s in c["sources"]} >= {"faq.csv", "billing_terms.md"}  # both are still disclosed by the engine, none is chosen
    r, provider = run(acme, "When are invoices due?")
    assert r["execution"]["answer_path"] != "fast_path" and len(provider.calls) == 1  # the model is consulted exactly as it was without a fast path


def test_outdated_information_is_never_used_and_never_fast_pathed(acme):
    r, _ = run(acme, "Within how many days of payment can I ask for a refund?", mode="deterministic")  # the 2025 policy (30 days) has expired; the current terms say 14
    assert "14" in (r["answer"] or "") and "30" not in (r["answer"] or "")
    r, provider = run(acme, "Within how many days of payment can I ask for a refund?")
    assert r["execution"]["answer_path"] != "fast_path"  # a passage, not an exact FAQ answer
    assert all("refund_policy_2025" not in (e["source"] or "") for e in r["evidence"]) and all("refund_policy_2025" not in (s["source"] or "") for s in r["sources"])  # the expired policy is not even evidence


def test_a_multi_part_question_is_fast_pathed_only_when_every_part_has_its_own_evidence(acme):
    r, provider = run(acme, "Can I export my data, and where is it stored?")
    assert r["execution"]["answer_path"] == "fast_path" and provider.calls == []
    ids = [c["evidence_ids"] for c in r["claims"]]
    assert len(ids) == 2 and ids[0] != ids[1] and all(ids)  # one cited answer per part
    assert "CSV or JSON" in r["answer"] and "Frankfurt" in r["answer"]
    pipe, st = state(acme, "Can I export my data, and how many moons does Jupiter have?")  # one part is answered, the other is nowhere in the knowledge
    d = pipe.evaluate_fast_path(st)
    assert st.base["outcome"] == "partial" and d.taken is False and d.reason == "not_eligible:partial/partial"
    r2, p2 = run(acme, "Can I export my data, and how many moons does Jupiter have?")
    assert r2["execution"]["answer_path"] != "fast_path" and len(p2.calls) == 1  # the pipeline continues; the deterministic engine's partial answer names what it could not answer
    assert "Jupiter" not in json.dumps([c["text"] for c in r2["claims"]])  # nothing was invented for the missing part


def test_insufficient_evidence_is_a_deterministic_abstention_and_no_model_is_asked_to_fill_the_gap(acme):
    for q in ("What is the capital of France?", "Is Acme Cloud HIPAA compliant?"):
        r, provider = run(acme, q)
        ex = r["execution"]
        assert r["outcome"] == "insufficient" and r["answer"] is None and r["verified"] is True and r["message"]
        assert ex["answer_path"] == "abstained" and ex["model_invoked"] is False and provider.calls == [] and ex["fast_path_reason"].startswith("not_eligible:insufficient")


def test_a_clarification_is_not_skipped(acme):
    r, provider = run(acme, "What about that?")
    assert r["outcome"] == "clarification" and r["execution"]["answer_path"] != "fast_path" and provider.calls == []


# ================================================================== a score is never the reason, and every condition can stop it
def test_a_very_high_similarity_does_not_make_a_meaning_match_an_exact_one(acme):
    pipe, st = state(acme, "Where do you keep customer data?")
    st.base["retrieval_similarity"] = 0.999
    st.base["answer_sufficiency"] = 1.0
    d = pipe.evaluate_fast_path(st)
    assert d.taken is False and d.reason == "not_eligible:faq/semantic"  # (the engine's own scores are not inputs to the decision)
    src = (ROOT / "src" / "faqengine" / "runtime" / "pipeline.py").read_text()
    body = src[src.index("def evaluate_fast_path"):src.index("def conclude_fast")]
    assert "similarity" not in body.replace("A similarity score is never an input", "")  # and the code that decides does not read one


def tamper(acme, mutate):
    pipe, st = state(acme, "Do you offer a free trial?")
    assert pipe.evaluate_fast_path(st).taken is True  # the untouched state qualifies
    mutate(st)
    return pipe.evaluate_fast_path(st)


def one_check(st, **kw):
    c = next(c for c in st.checks if c.evidence_id in st.accepted and c.status == "accepted")
    st.checks[st.checks.index(c)] = dataclasses.replace(c, **kw)


@pytest.mark.parametrize("name,mutate,reason", [
    ("a clarification is needed", lambda st: st.base.update(clarification={"options": ["a", "b"]}), "declined:needs_clarification"),
    ("accepted sources disagree", lambda st: st.base.update(conflicts=[{"sources": []}]), "declined:unresolved_conflict"),
    ("a part is left unanswered", lambda st: st.base.update(unanswered_parts=["the second part"]), "declined:part_unanswered"),
    ("the evidence is only context, not accepted", lambda st: one_check(st, status="context"), "declined:sufficiency_not_confirmed:INSUFFICIENT"),
    ("a requirement of the plan has no evidence", lambda st: st.plan.requirements.append("how many moons does Jupiter have"), "declined:sufficiency_not_confirmed:PARTIAL"),
    ("a part of the question has no requirement behind it", lambda st: st.plan.parts.extend(["something else", "and another"]), "declined:requirement_not_covered"),
    ("the cited evidence has expired", lambda st: one_check(st, reasons=["expired on 2025-12-31"]), "declined:evidence_not_current"),
    ("the cited evidence was replaced", lambda st: one_check(st, reasons=["superseded by another source"]), "declined:evidence_not_current"),
])
def test_each_condition_alone_stops_the_fast_path_with_its_own_reason(acme, name, mutate, reason):
    d = tamper(acme, mutate)
    assert d.taken is False and d.reason == reason, (name, d.reason)


def test_evidence_that_belongs_to_another_business_stops_the_fast_path(acme):
    def foreign(st):
        for eid in list(st.by_id):
            st.by_id[eid] = dataclasses.replace(st.by_id[eid], tenant_id="t-2")
    d = tamper(acme, foreign)
    assert d.taken is False and d.reason == "declined:evidence_of_another_tenant"


def test_a_deterministic_answer_that_fails_claim_or_citation_verification_stops_the_fast_path(acme, monkeypatch):
    pipe, st = state(acme, "Do you offer a free trial?")
    real = claimcheck.check
    monkeypatch.setattr(claimcheck, "check", lambda *a, **k: dataclasses.replace(real(*a, **k), passed=False))
    d = pipe.evaluate_fast_path(st)
    assert d.taken is False and d.reason == "declined:deterministic_answer_not_verified"
    st2 = state(acme, "Do you offer a free trial?")[1]
    monkeypatch.setattr(claimcheck, "check", real)
    pipe2 = Pipeline("byok", Counting(), AllowAll(), fast_path=True)
    comp = st2.det.compose(st2.plan, st2.usable, st2.suff, st2.base)
    comp.claims[0].evidence_ids = []  # a claim that cites nothing
    monkeypatch.setattr(st2.det.__class__, "compose", lambda self, *a, **k: comp)
    assert pipe2.evaluate_fast_path(st2).reason == "declined:deterministic_answer_not_verified"


def test_when_the_fast_path_declines_the_request_continues_exactly_as_without_one(acme):
    q = "Where do you keep customer data?"
    with_fast, p1 = run(acme, q, fast=True)
    without, p2 = run(acme, q, fast=False)
    assert with_fast["answer"] == without["answer"] and with_fast["claims"] == without["claims"] and len(p1.calls) == len(p2.calls) == 1
    assert with_fast["execution"]["answer_path"] == without["execution"]["answer_path"] == "model"
    assert without["execution"]["fast_path_reason"] == "disabled" and with_fast["execution"]["fast_path_reason"] == "not_eligible:faq/semantic"


# ================================================================== the switch, the modes, the record
def test_with_the_switch_off_an_exact_faq_answer_goes_to_the_model_as_it_did_before(acme):
    r, provider = run(acme, "Do you offer a free trial?", fast=False)
    assert len(provider.calls) == 1 and r["execution"]["answer_path"] == "model" and r["execution"]["model_invoked"] is True and r["execution"]["fast_path_reason"] == "disabled"


def test_a_deterministic_business_has_no_model_to_skip_and_says_so(acme):
    r, provider = run(acme, "Do you offer a free trial?", mode="deterministic")
    ex = r["execution"]
    assert provider.calls == [] and ex["answer_path"] == "deterministic" and ex["fast_path_reason"] == "not_applicable:deterministic_mode" and ex["model_invoked"] is False


def test_a_test_doubles_composer_is_left_alone_even_when_the_fast_path_is_on(acme):
    from faqengine.runtime.contracts import Composition

    class Mock:
        name = "mock"
        calls = 0

        def compose(self, plan, evidence, sufficiency, base):
            Mock.calls += 1
            return Composition([], "insufficient", "", "mock", base)

    search, sim = acme
    r = Pipeline("byok", Counting(), AllowAll(), composer=Mock(), fast_path=True).run("Do you offer a free trial?", search, sim, as_of=DAY)
    assert Mock.calls == 1 and r["execution"]["answer_path"] == "mock" and r["execution"]["generated_by"] == "mock"  # an exact FAQ question, and still the double's composition


def test_the_execution_record_says_how_every_answer_was_made(acme, beacon):
    for world, q in ((acme, "Do you offer a free trial?"), (acme, "Where do you keep customer data?"), (acme, "What is the capital of France?"), (beacon, "How much would 12 extra agents cost on the Growth plan?"),
                     (acme, "When are invoices due?")):
        r, _ = run(world, q)
        ex = r["execution"]
        assert set(("answer_path", "fast_path_reason", "model_invoked", "provider_steps_tried", "evidence_ids", "verification", "latency_ms")) <= set(ex), q
        assert ex["answer_path"] in ("fast_path", "model", "deterministic", "abstained", "deterministic_fallback") and isinstance(ex["model_invoked"], bool) and isinstance(ex["evidence_ids"], list)
        assert set(ex["verification"]) == {"outcome", "passed", "claims_checked", "claims_removed"} and ex["fast_path_reason"]
        assert (ex["model_invoked"] is False) == (ex["provider_steps_tried"] == [])
