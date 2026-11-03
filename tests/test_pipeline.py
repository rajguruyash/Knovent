"""The model-independent reasoning pipeline (runtime/pipeline.py and the stages it calls), layer by layer and end to end. No model, no network: providers and consent are test doubles
(runtime/mocks.py), so nothing here says anything about how a real language model performs."""
import dataclasses
import logging
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
import claimcheck  # noqa: E402
import compose  # noqa: E402
import evidence_check  # noqa: E402
import mocks  # noqa: E402
import pipeline  # noqa: E402
import sufficiency  # noqa: E402
import targeted  # noqa: E402
import understand  # noqa: E402
from contracts import Claim, Composition, ProviderReply  # noqa: E402
from pipeline_cases import EVIDENCE_CASES, KNOWN, QUERY_CASES, Sim, hit  # noqa: E402

DAY = date(2026, 6, 1)


# ---------- stage 1: query understanding
@pytest.mark.parametrize("question,want", QUERY_CASES)
def test_the_query_plan_keeps_what_the_question_asks(question, want):
    p = understand.understand(question, KNOWN, today=DAY)
    if "intent" in want:
        assert p.intent == want["intent"]
    if "conditions" in want:
        assert {c.text.lower() for c in p.conditions} == want["conditions"] if "include" in " ".join(want["conditions"]) else want["conditions"] <= {c.text.lower() for c in p.conditions}
    if "qualifiers" in want:
        assert want["qualifiers"] <= set(p.qualifiers)
    if "claim_types" in want:
        assert {c.claim_type for c in p.conditions if c.claim_type} == want["claim_types"]
    if "qualifier_on_condition" in want:
        assert any(c.qualifier == want["qualifier_on_condition"] for c in p.conditions)
    if "comparison" in want:
        assert p.comparison["operation"] == want["comparison"]
    if "needs_structured" in want:
        assert p.needs_structured is want["needs_structured"]
    if "entities" in want:
        assert want["entities"] <= {e.lower() for e in p.entities}
    if "day" in want:
        assert p.temporal["day"] == want["day"]
    if "parts" in want:
        assert len(p.parts) == want["parts"]


def test_both_conditions_are_kept_and_requirements_name_each():
    p = understand.understand("Which plans support both WhatsApp and SSO?", KNOWN)
    assert p.requirements == ["WhatsApp", "SSO"] and "both" in p.qualifiers and p.multi_source


def test_an_interpreter_can_add_to_a_plan_and_never_remove_from_it():
    class Interp:
        def interpret(self, q, plan):
            return {"conditions": ["audit log"], "entities": ["Scale"], "qualifiers": [], "bogus": 1, "attributes": "not a list"}

    p = understand.understand("Which plans support both WhatsApp and SSO?", KNOWN, Interp())
    assert {c.text for c in p.conditions} == {"WhatsApp", "SSO", "audit log"} and "Scale" in p.entities and p.interpreted_by.endswith("interpreter")
    assert understand.merge(p, "garbage") is p and understand.merge(p, None) is p


# ---------- stages 3 and 4: normalisation and validation
@pytest.mark.parametrize("label,question,h,status", EVIDENCE_CASES)
def test_validation_accepts_what_states_the_claim_and_not_what_comes_near(label, question, h, status):
    plan = understand.understand(question, KNOWN, today=DAY)
    e = evidence_check.normalize([h], "t1")
    c = evidence_check.validate(plan, e, "t1", DAY, Sim())[0]
    assert c.status == status, (label, c.reasons)


def test_evidence_ids_are_made_once_and_never_change():
    hits = [hit("x1", "A", "a"), hit("x2", "B", "b")]
    first = evidence_check.normalize(hits, "t1")
    assert [e.id for e in first] == ["E1", "E2"] and first[0].tenant_id == "t1" and first[0].source_name == "plans.csv"
    more = evidence_check.normalize([hit("x2", "B", "b"), hit("x3", "C", "c")], "t1", start=3, known={e.entry_id: e.id for e in first})
    assert [(e.id, e.entry_id) for e in more] == [("E3", "x3")]  # an entry already numbered keeps its number and is not made twice
    with pytest.raises(dataclasses.FrozenInstanceError):
        first[0].id = "E9"  # frozen


def test_similarity_is_not_support():
    plan = understand.understand("Is there an annual discount?", KNOWN)
    e = evidence_check.normalize([hit("n", "Billing", "Annual billing is available on request.", "billing.md", score=0.99, combined_score=0.99)], "t1")
    assert evidence_check.validate(plan, e, "t1", DAY, Sim())[0].status == "context"  # a near-perfect score changes nothing


# ---------- stage 5: sufficiency
def _suff(question, hits, **kw):
    plan = understand.understand(question, KNOWN, today=DAY)
    e = evidence_check.normalize(hits, "t1")
    c = evidence_check.validate(plan, e, "t1", DAY, Sim())
    return sufficiency.assess(plan, e, c, **kw), plan


def test_sufficiency_has_five_outcomes():
    s, _ = _suff("How long is the refund window?", [hit("h", "Refunds", "Customers can request a full refund within 30 days of purchase.", "policies.md")])
    assert s.status == "SUFFICIENT" and s.action == "answer" and not s.unsupported
    s, _ = _suff("Is there an annual discount?", [hit("b", "Billing", "Annual billing is available on request.", "billing.md")])
    assert s.status == "INSUFFICIENT" and s.action == "refuse" and s.needs_retrieval is False or s.gap_queries is not None
    s, _ = _suff("Can I request a refund and is there a free trial?", [hit("h", "Refunds", "Customers can request a full refund within 30 days of purchase.", "policies.md")])
    assert s.status == "PARTIAL" and s.action == "qualify" and any("trial" in u for u in s.unsupported) and s.gap_queries
    s, _ = _suff("How long is the refund window?", [hit("h", "Refunds", "Refunds within 30 days.", "p.md")], conflicts=[{"reason": "one says 14 and the other 30"}])
    assert s.status == "CONFLICTED" and s.action == "qualify"
    s, _ = _suff("and that?", [], clarification={"reason": "the question does not say what it is about"})
    assert s.status == "AMBIGUOUS" and s.action == "refuse"


def test_conditions_that_hold_separately_are_not_one_thing_that_has_them_all():
    hits = [hit("w", "WhatsApp", "WhatsApp inbox, available on: Growth.", "features.xlsx"), hit("s", "SSO", "SSO feature, available on: Scale.", "features.xlsx")]
    s, plan = _suff("Which plans support both WhatsApp and SSO?", hits)
    assert s.status == "PARTIAL" and s.unverified_conditions == ["all of: WhatsApp and SSO"] and "not established" in " ".join(s.notes)


# ---------- stage 6: targeted retrieval
def test_the_second_pass_runs_once_asks_only_for_what_is_missing_and_never_repeats_a_query():
    asked = {"how much is growth and is there a trial"}
    calls = []

    def search(q):
        calls.append(q)
        return [hit("t1", "Trial", "Every plan has a 14 day free trial.", "faq.csv", kind="faq")], [], {}

    new, run = targeted.second_pass(["free trial Growth", "free trial Growth", "how much is growth and is there a trial", "card needed", "third"], asked, search, {"already"})
    assert len(run) == 2 and len(calls) == 2 and [h["found_by"] for h in new] == ["targeted"] and len(new) == 1  # one pass, at most two queries, duplicates dropped
    again, run2 = targeted.second_pass(["free trial Growth"], asked, search, {"already", "t1"})
    assert run2 == [] and again == []  # the same query is not asked again


# ---------- stage 8: claim verification
def _ev(*items):
    e = evidence_check.normalize(list(items), "t1")
    return {x.id: x for x in e}, {x.id for x in e}


def test_a_supported_claim_with_its_citation_passes():
    by, ok = _ev(hit("a", "Refunds", "Customers can request a full refund within 30 days of purchase.", "p.md"))
    r = claimcheck.check([Claim("Customers can request a full refund within 30 days of purchase.", ["E1"])], by, ok, "t1")
    assert r.passed and r.claims[0]["status"] == "verified"


@pytest.mark.parametrize("claim,issue", [
    (Claim("There is an annual discount.", ["E1"]), "different_claim"),   # annual billing is available is not an annual discount
    (Claim("Refunds take 5 days.", ["E9"]), "fabricated_evidence_id"),
    (Claim("Refunds take 5 days.", []), "no_citation"),
    (Claim("Customers can request a full refund within 60 days of purchase.", ["E2"]), "changed_number"),
    (Claim("Every plan gets a refund within 30 days of purchase.", ["E2"]), "scope_expanded"),
    (Claim("Customers can request a full refund.", ["E3"]), "citation_not_accepted"),
])
def test_claims_that_the_evidence_does_not_carry_are_found(claim, issue):
    by, ok = _ev(hit("a", "Billing", "Annual billing is available on request.", "b.md"), hit("b", "Refunds", "The Growth plan can be refunded within 30 days of purchase.", "p.md"),
                 hit("c", "Refunds", "Customers can request a full refund within 30 days.", "p2.md"))
    ok.discard("E3")
    r = claimcheck.check([claim], by, ok, "t1")
    assert not r.passed and issue in {i["type"] for i in r.issues}, r.issues


def test_a_citation_to_another_businesss_evidence_fails_even_if_the_text_matches():
    by, ok = _ev(hit("a", "Refunds", "Customers can request a full refund within 30 days of purchase.", "p.md", tenant_id="other"))
    r = claimcheck.check([Claim("Customers can request a full refund within 30 days of purchase.", ["E1"])], by, ok, "t1")
    assert "cross_tenant" in {i["type"] for i in r.issues}


def test_a_condition_the_cited_sentence_carries_must_survive():
    by, ok = _ev(hit("a", "Refunds", "Refunds are only available if requested within 14 days of the first payment.", "p.md"))
    r = claimcheck.check([Claim("Refunds are available within 14 days.", ["E1"])], by, ok, "t1")
    assert not r.passed and {"missing_condition", "misrepresented_policy"} & {i["type"] for i in r.issues}


def test_one_plan_is_not_every_plan_and_an_enterprise_sla_is_not_every_deployment():
    by, ok = _ev(hit("a", "Features", "SSO is available on Scale and Enterprise.", "f.md"), hit("b", "SLA", "Enterprise customers receive a 99.9% uptime commitment.", "s.md"))
    assert not claimcheck.check([Claim("SSO is available on all plans.", ["E1"])], by, ok, "t1").passed
    assert not claimcheck.check([Claim("Every Enterprise deployment has a 99.9% uptime commitment.", ["E2"])], by, ok, "t1").passed
    assert claimcheck.check([Claim("SSO is available on Scale and Enterprise.", ["E1"])], by, ok, "t1").passed


def test_calculated_claims_may_use_only_cited_rows_and_computed_numbers():
    by, ok = _ev(hit("a", "Team", "Team plan, monthly price: 29 USD.", "plans.csv"))
    assert claimcheck.check([Claim("3 × 29 USD = 87 USD.", ["E1"], "calc")], by, ok, "t1", {"87"}, "How much would 3 Team plans cost?").passed
    r = claimcheck.check([Claim("3 × 29 USD = 90 USD.", ["E1"], "calc")], by, ok, "t1", {"87"}, "How much would 3 Team plans cost?")
    assert not r.passed and r.issues[0]["type"] == "changed_number"


def test_a_qualifier_states_no_fact_and_may_not_add_a_number():
    by, ok = _ev(hit("a", "Team", "Team plan, monthly price: 29 USD.", "plans.csv"))
    assert claimcheck.check([Claim("Reading: the listed price is the price of one month.", [], "qualifier")], by, ok, "t1").passed
    assert not claimcheck.check([Claim("Reading: taxes add 18 percent.", [], "qualifier")], by, ok, "t1").passed


def test_resolve_removes_the_claim_that_failed_and_never_calls_the_rest_verified():
    by, ok = _ev(hit("a", "Refunds", "Customers can request a full refund within 30 days of purchase.", "p.md"))
    comp = Composition([Claim("Customers can request a full refund within 30 days of purchase.", ["E1"]), Claim("There is also a student discount.", ["E1"])], "answered", "x", "mock")
    report = claimcheck.check(comp.claims, by, ok, "t1")
    final = claimcheck.resolve(comp, report, by)
    assert final.verdict == "partial" and "student discount" not in final.answer and "left out" in final.answer
    none = claimcheck.resolve(Composition([Claim("A student discount applies.", ["E1"])], "answered", "x", "mock"), claimcheck.check([Claim("A student discount applies.", ["E1"])], by, ok, "t1"), by)
    assert none.verdict == "insufficient" and not none.claims


# ---------- the composers
def test_a_model_composer_tells_a_provider_failure_from_a_reasoning_failure():
    plan = understand.understand("How long is the refund window?", KNOWN)
    e = evidence_check.normalize([hit("a", "Refunds", "Customers can request a full refund within 30 days of purchase.", "p.md")], "t1")
    s = sufficiency.assess(plan, e, evidence_check.validate(plan, e, "t1", DAY))
    run = lambda reply: compose.ModelComposer(mocks.MockProvider([reply])).compose(plan, e, s, {})  # noqa: E731
    assert run(ProviderReply(failure="provider:timeout")).failure == "provider:timeout"
    assert run(ProviderReply(failure="provider:rate_limited")).failure == "provider:rate_limited"
    assert run(ProviderReply(text="Sure, here you go!")).failure == "reasoning:invalid_json"
    assert run(ProviderReply(text='{"verdict": "maybe", "claims": []}')).failure == "reasoning:schema"
    assert run(ProviderReply(text='{"verdict": "answered", "claims": [{"text": 5}]}')).failure == "reasoning:malformed_claim"
    ok = run(mocks.reply_json("answered", [("Customers can request a full refund within 30 days of purchase.", ["E1"])]))
    assert ok.failure is None and ok.claims[0].evidence_ids == ["E1"]


# ---------- end to end, with a hand-made search
REFUND = hit("pol-1", "Refunds", "Customers can request a full refund within 30 days of purchase.", "policies.md", matched="Refunds", matched_kind="q")
BILLING = hit("bill-1", "Billing", "Annual billing is available on request.", "billing.md", matched="Billing", matched_kind="q")
TRIAL = hit("faq-1", "Do you offer a free trial?", "Every plan has a 14 day free trial.", "faq.csv", kind="faq", matched="Do you offer a free trial?", matched_kind="q")


def make_search(*hits, on_query=None):
    def search(q, on=None):
        if on_query:
            on_query(q)
        faq = [h for h in hits if h["kind"] == "faq"]
        return faq, [h for h in hits if h["kind"] != "faq"], {"method": "test"}
    return search


def run(question, *hits, pipe=None, **kw):
    return (pipe or pipeline.Pipeline(tenant_id="t1")).run(question, make_search(*hits, **kw), Sim(), as_of=DAY)


def test_deterministic_mode_answers_verifies_and_says_how():
    r = run("How long is the refund window?", REFUND)
    ex = r["execution"]
    assert r["outcome"] == "grounded" and "30 days" in r["answer"] and r["verified"] and r["sufficiency"]["status"] == "SUFFICIENT"
    assert ex["mode"] == "deterministic" and ex["generated_by"] == "deterministic" and ex["inference_attempted"] is False and ex["provider"] is None and ex["retrieval_passes"] == 1
    assert {"understand", "validate", "engine_compose", "verify", "total"} <= set(ex["stage_ms"])
    assert r["claims"][0]["evidence_ids"] == ["E1"] and r["query_plan"]["intent"] and r["evidence"][0]["status"] == "accepted"


def test_an_annual_billing_statement_does_not_answer_an_annual_discount_question():
    r = run("Is there an annual discount?", BILLING)
    assert r["outcome"] == "insufficient" and r["answer"] is None and r["evidence"][0]["status"] == "context" and r["sufficiency"]["status"] == "INSUFFICIENT"


def test_expired_and_superseded_and_foreign_evidence_never_answers():
    old = hit("old-1", "Refunds", "Refunds within 14 days.", "old.md", inactive="expired on 2025-12-31")
    foreign = hit("for-1", "Refunds", "Refunds within 90 days.", "other.md", tenant_id="someone-else")
    r = run("How long is the refund window?", old, foreign, REFUND)
    assert "30 days" in r["answer"] and "14" not in r["answer"] and "90" not in r["answer"]
    st = {e["entry_id"]: e["status"] for e in r["evidence"]}
    assert st["old-1"] == "rejected" and st["for-1"] == "rejected" and st["pol-1"] == "accepted"
    assert "cross_tenant" in " ".join(next(e for e in r["evidence"] if e["entry_id"] == "for-1")["reasons"])


def test_empty_evidence_is_an_abstention_not_a_failure():
    r = run("How long is the refund window?")
    assert r["outcome"] == "insufficient" and r["sufficiency"]["status"] == "INSUFFICIENT" and r["execution"]["validation"] == "verified"


def test_contradictory_evidence_is_disclosed_never_resolved():
    a = hit("f-5", "Do prices include VAT?", "Yes. All prices on the pricing page include VAT.", "faq.csv", kind="faq", matched="Do prices include VAT?", matched_kind="q")
    b = hit("p-3", "Taxes", "All prices exclude VAT.", "policies.md")
    r = run("Do the prices already include VAT?", a, b)
    assert r["outcome"] == "conflict" and r["sufficiency"]["status"] == "CONFLICTED" and r["sufficiency"]["action"] == "qualify" and r["verified"]


def test_a_partial_answer_names_what_is_missing_and_one_more_search_is_tried_once():
    queries = []
    r = run("Can I request a refund and is there a free trial?", REFUND, on_query=queries.append)
    assert r["outcome"] == "partial" and "30 days" in r["partial_answer"] and any("trial" in u for u in r["unanswered_parts"])
    assert r["execution"]["retrieval_passes"] == 2 and 2 <= len(queries) <= 3  # the question, then at most two targeted queries, once
    found = run("Can I request a refund and is there a free trial?", REFUND, TRIAL)
    assert found["execution"]["retrieval_passes"] == 1 or found["outcome"] in ("grounded", "partial")


# ---------- execution modes, consent and failure
def model_answer(text="Customers can request a full refund within 30 days of purchase.", ids=("E1",)):
    return mocks.reply_json("answered", [(text, list(ids))])


def test_without_consent_no_provider_is_ever_called():
    prov = mocks.MockProvider([model_answer()])
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", prov, mocks.MockConsent(), tenant_id="t1"))
    assert prov.calls == [] and r["execution"]["generated_by"] == "deterministic_no_consent" and r["execution"]["failure_kind"] == "consent:missing_or_revoked" and r["execution"]["inference_attempted"] is False
    assert "30 days" in r["answer"] and r["execution"]["fallback"] is False


def test_with_consent_a_verified_model_answer_is_used_and_said_to_be_the_models():
    prov = mocks.MockProvider([model_answer()])
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("byok", prov, mocks.MockConsent({("t1", "mock")}), tenant_id="t1"))
    ex = r["execution"]
    assert len(prov.calls) == 1 and ex["generated_by"] == "model" and ex["inference_attempted"] and ex["inference_succeeded"] and not ex["fallback"] and r["verified"] and "E1" not in r["answer"] or r["verified"]
    assert ex["provider"] == "mock" and ex["model"] == "mock-1"


def test_revoking_consent_stops_the_next_call():
    prov, consent = mocks.MockProvider([model_answer()]), mocks.MockConsent({("t1", "mock")})
    pipe = pipeline.Pipeline("platform", prov, consent, tenant_id="t1")
    run("How long is the refund window?", REFUND, pipe=pipe)
    consent.revoke("t1", "mock")
    r = run("How long is the refund window?", REFUND, pipe=pipe)
    assert len(prov.calls) == 1 and r["execution"]["generated_by"] == "deterministic_no_consent"


def test_consent_is_per_business():
    prov = mocks.MockProvider([model_answer()])
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", prov, mocks.MockConsent({("someone-else", "mock")}), tenant_id="t1"))
    assert prov.calls == [] and r["execution"]["generated_by"] == "deterministic_no_consent"


@pytest.mark.parametrize("reply,kind", [(ProviderReply(failure="provider:timeout"), "provider:timeout"), (ProviderReply(failure="provider:rate_limited"), "provider:rate_limited"),
                                        (ProviderReply(text="not json"), "reasoning:invalid_json"), (ProviderReply(text='{"verdict": "answered"}'), "reasoning:schema")])
def test_a_failing_provider_or_a_bad_answer_falls_back_openly(reply, kind):
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", mocks.MockProvider([reply]), mocks.MockConsent({("t1", "mock")}), tenant_id="t1"))
    ex = r["execution"]
    assert ex["fallback"] and ex["failure_kind"] == kind and ex["generated_by"] == "deterministic_fallback" and ex["inference_succeeded"] is False and "30 days" in r["answer"]


def test_without_permission_to_fall_back_a_failure_is_reported_not_covered_up():
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", mocks.MockProvider([ProviderReply(failure="provider:server")]), mocks.MockConsent({("t1", "mock")}),
                                                                        allow_fallback=False, tenant_id="t1"))
    assert r["outcome"] == "insufficient" and r["answer"] is None and "temporarily unavailable" in r["message"] and r["execution"]["generated_by"] == "unavailable" and r["verified"] is False


def test_a_model_claim_the_evidence_does_not_carry_is_never_passed_on():
    bad = mocks.reply_json("answered", [("Customers can request a full refund within 30 days of purchase.", ["E1"]), ("Students get a discount.", ["E1"])])
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", mocks.MockProvider([bad]), mocks.MockConsent({("t1", "mock")}), allow_fallback=False, tenant_id="t1"))
    assert "Students" not in (r.get("answer") or r.get("partial_answer") or "") and r["verified"] is False and r["claim_verification"]["removed"] == 1 and r["outcome"] == "partial"
    r2 = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", mocks.MockProvider([bad]), mocks.MockConsent({("t1", "mock")}), tenant_id="t1"))
    assert r2["execution"]["fallback"] and r2["execution"]["failure_kind"] == "reasoning:unsupported_claims" and r2["execution"]["generated_by"] == "deterministic_fallback" and r2["verified"]


def test_a_fabricated_evidence_id_from_a_model_is_rejected():
    prov = mocks.MockProvider([model_answer(ids=("E7",))])
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", prov, mocks.MockConsent({("t1", "mock")}), allow_fallback=False, tenant_id="t1"))
    assert any(i["type"] == "fabricated_evidence_id" for i in r["claim_verification"]["issues"]) and r["verified"] is False


def test_a_mock_composer_is_labelled_a_mock_and_checked_like_any_other():
    m = compose.MockComposer([Claim("Customers can request a full refund within 30 days of purchase.", ["E1"])])
    r = run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline(composer=m, tenant_id="t1"))
    assert r["execution"]["generated_by"] == "mock" and r["verified"]
    cited = compose.MockComposer([Claim("Customers can request a full refund within 30 days of purchase.", ["E2"])])
    assert not run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline(composer=cited, tenant_id="t1"))["verified"]


def test_structured_results_are_not_given_to_a_model_to_restate(tmp_path):
    from faqengine import sources

    (tmp_path / "plans.csv").write_text("Plan,Monthly price (USD),Agents\nFree,0,1\nTeam,29,5\nGrowth,79,20\n")
    ents = sources.load_sources([tmp_path / "plans.csv"]).entries
    hits = [hit(e.id, e.question, e.answer, "plans.csv", record=e.record) for e in ents]
    import structured

    model = structured.Model([{"entry_id": h["entry_id"], "question": h["question"], "answer": h["answer"], "kind": "passage", "source": "plans.csv", "record": h["record"]} for h in hits])

    def search(q, on=None):
        return [], hits, {"method": "test"}

    search.model = lambda: model
    prov = mocks.MockProvider([model_answer()])
    r = pipeline.Pipeline("platform", prov, mocks.MockConsent({("t1", "mock")}), tenant_id="t1").run("Which plan is the cheapest?", search, None, as_of=DAY)
    assert prov.calls == [] and "Free has the lowest Monthly price: 0 USD" in r["answer"] and r["execution"]["inference_attempted"] is False and r["verified"]
    assert r["query_plan"]["intent"] == "superlative" and r["sufficiency"]["status"] == "SUFFICIENT" and all(c["evidence_ids"] for c in r["claims"] if c["kind"] in ("fact", "calc"))
    wrong = compose.MockComposer([Claim("Free has the lowest Monthly price: 1 USD.", ["E1"], "calc")])
    assert not pipeline.Pipeline(composer=wrong, tenant_id="t1").run("Which plan is the cheapest?", search, None, as_of=DAY)["verified"]


def test_nothing_that_is_logged_contains_document_text(caplog):
    with caplog.at_level(logging.DEBUG):
        run("How long is the refund window?", REFUND, pipe=pipeline.Pipeline("platform", mocks.MockProvider([ProviderReply(failure="provider:timeout")]), mocks.MockConsent({("t1", "mock")}), tenant_id="t1"))
    assert "refund" not in caplog.text.lower()


def test_unknown_modes_are_refused():
    with pytest.raises(ValueError):
        pipeline.Pipeline("cloud")
