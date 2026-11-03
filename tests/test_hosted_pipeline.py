"""Phase H: the reasoning pipeline in the hosted service, behind a flag, and the consent that gates every external inference call.

Real schema (every migration, PostgreSQL with pgvector), the real embedding model, the whole Acme and Beacon examples ingested through the service. Parity is MEASURED here, on stored passages and
stored vectors: the existing path and the pipeline answer the same requests, and every difference is read, not assumed away. Only the dev and regression sets are used; the locked and fresh sets are
never asked.

No external inference provider is connected to this product. The security tests inject a mock provider (`provider_factory`, which nothing in the product sets) to prove that consent is what stands
between a business's text and a provider."""
import csv
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
pytest.importorskip("cryptography")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "scripts"))
from pgdb import make_db  # noqa: E402

import unified_benchmark as UB  # noqa: E402
from faqengine.hosted import disclosure  # noqa: E402
from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted.secrets import SecretBox  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402
from faqengine.runtime.mocks import MockProvider, reply_json  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
pytestmark = pytest.mark.skipif(not MODEL.is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")
KNOWLEDGE = ROOT / "examples" / "unified" / "knowledge"
REASONING = ROOT / "examples" / "reasoning"
DAY = date(2026, 6, 1)
PIPELINE_KEYS = {"query_plan", "evidence", "sufficiency", "claim_verification", "verified"}
HOURS = "Do you offer a free trial?"



@pytest.fixture(autouse=True)
def _verified(verified_providers):
    """These tests are about what happens once a provider's terms are verified; the real entries are all unverified (tests/test_ai_disclosure.py, and the unverified tests below, say what that does)."""
    yield


class Files(dict):
    def __call__(self, path):
        if path not in self:
            raise svc.ServiceError(404, "the uploaded file could not be read from storage")
        return self[path]


@pytest.fixture(scope="module")
def db():
    yield from make_db()


@pytest.fixture(scope="module")
def clock():
    return {"day": DAY}


@pytest.fixture(scope="module")
def engine(db, clock):
    box = SecretBox({1: SecretBox.generate() and __import__("base64").b64decode(SecretBox.generate())})
    return svc.HostedEngine(Store(db.dsn), Embedder(MODEL), box, Files(), rate_per_minute=100_000, today=lambda: clock["day"], allow_private_llm=True, ai_modes={"byok"}, fast_path=False)  # these tests are about the MODEL path (consent, routing, limits, metering, verification): the deterministic fast path has its own tests (test_fast_path.py)


@pytest.fixture(autouse=True)
def flag_off(engine):
    """Every test starts from the shipped setting, and leaves it so."""
    engine.pipeline_mode, engine.provider_factory = "off", None
    engine.stats.clear()
    for d in engine.latency.values():
        d.clear()
    engine._recorded = {}
    yield
    engine.pipeline_mode, engine.provider_factory = "off", None


def add(db, engine, business, owner, name, data, kind="document"):
    engine.fetch_object[f"{business}/{name}"] = data
    sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, %s, %s, %s) returning id", (business, name, kind, f"{business}/{name}"))[0][0])
    return sid, engine.ingest(business, owner, sid)


def ask(engine, business, owner, question):
    return engine.respond(business, owner, question)["result"]


def rows_of(path):
    return [dict(r) for r in csv.DictReader(open(path, encoding="utf-8"))]


@pytest.fixture(scope="module")
def acme(db, engine):
    owner = str(db.user("p-owner"))
    biz = str(db.business(owner, "acme-pipeline"))
    for p in sorted(KNOWLEDGE.rglob("*")):
        if p.is_file() and p.suffix.lower() in UB.sources.SUPPORTED:
            add(db, engine, biz, owner, p.name, p.read_bytes(), "faq_table" if p.name == "faq.csv" else "document")
    editor, viewer = str(db.user("p-editor")), str(db.user("p-viewer"))
    db.member(biz, editor, "editor")
    db.member(biz, viewer, "viewer")
    return type("A", (), {"biz": biz, "owner": owner, "editor": editor, "viewer": viewer})


@pytest.fixture(scope="module")
def beacon(db, engine):
    owner = str(db.user("p-beacon"))
    biz = str(db.business(owner, "beacon-pipeline"))
    for p in sorted((REASONING / "knowledge").iterdir()):
        if p.suffix.lower() in UB.sources.SUPPORTED:
            add(db, engine, biz, owner, p.name, p.read_bytes(), "faq_table" if p.name == "faq.csv" else "document")
    return type("B", (), {"biz": biz, "owner": owner})


@pytest.fixture(scope="module")
def other(db, engine):
    """A second business with its own knowledge and its own people."""
    owner = str(db.user("p-other-owner"))
    biz = str(db.business(owner, "groomer-pipeline"))
    add(db, engine, biz, owner, "pets.csv", b"id,question,answer\np1,Do you groom cats?,We groom dogs only.\np2,What are your prices?,A bath costs 600 rupees.\n", "faq_table")
    return type("O", (), {"biz": biz, "owner": owner})


def audit(db, biz, action):
    return [r[0] for r in db.su("select detail from public.audit_logs where business_id = %s and action = %s order by created_at", (biz, action))]


# ---------------------------------------------------------------- the flag and the contract
def test_the_flag_is_off_by_default_and_a_wrong_value_stops_the_service():
    assert svc.HostedEngine.__init__.__defaults__ is not None
    with pytest.raises(ValueError):
        svc.HostedEngine(None, None, pipeline_mode="sometimes")
    with pytest.raises(SystemExit, match="ENGINE_PIPELINE"):
        svc.from_env({"DATABASE_URL": "x", "ENGINE_SERVICE_SECRET": "s" * 30, "EMBEDDING_MODEL_DIR": str(MODEL), "ENGINE_PIPELINE": "maybe"})


def test_off_keeps_the_existing_answer_and_says_which_path_made_it(engine, acme):
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert r["outcome"] in ("faq", "grounded") and r["execution"]["path"] == "legacy" and r["execution"]["mode"] == "deterministic"
    assert r["execution"]["generated_by"] == "deterministic" and r["execution"]["inference_attempted"] is False
    assert not (PIPELINE_KEYS & set(r))  # the response is the existing one, plus the one `execution` line
    assert engine.stats["legacy_answered"] == 1 and not engine.stats["pipeline_answered"]


def test_on_answers_with_the_pipeline_and_keeps_the_public_contract(engine, acme, db):
    off = ask(engine, acme.biz, acme.owner, HOURS)
    engine.pipeline_mode = "on"
    out = engine.respond(acme.biz, acme.owner, HOURS)
    r = out["result"]
    assert set(out) == {"result", "conversation_id", "message_id"}
    assert set(off) - {"latency_ms"} <= set(r)  # nothing the existing contract had is gone
    assert r["execution"]["path"] == "pipeline" and r["execution"]["mode"] == "deterministic" and r["execution"]["generated_by"] == "deterministic"
    assert r["execution"]["inference_attempted"] is False and r["execution"]["provider"] is None and r["execution"]["failure_kind"] is None
    assert r["outcome"] == off["outcome"] and r["answer"] == off["answer"]
    for s in r["sources"]:  # citations still point at this business's stored rows
        assert s["db_id"] and str(db.su("select business_id from public.faqs where id = %s union all select business_id from public.document_chunks where id = %s", (s["db_id"], s["db_id"]))[0][0]) == acme.biz
    assert {"understand", "validate", "compose", "verify", "total"} <= set(r["execution"]["stage_ms"]) and r["execution"]["retrieval_passes"] in (1, 2)
    stored = db.su("select llm -> 'execution' ->> 'generated_by', llm -> 'execution' ->> 'path', evidence -> 'sufficiency' ->> 'status', jsonb_array_length(evidence -> 'evidence') from public.messages where id = %s",
                   (out["message_id"],))[0]
    assert stored[:2] == ("deterministic", "pipeline") and stored[2] and stored[3] >= 1  # what was decided is kept with the exchange
    blob = db.su("select llm::text || evidence::text from public.messages where id = %s", (out["message_id"],))[0][0]
    assert "api_key" not in blob and "Authorization" not in blob


def test_the_debugging_view_has_every_stage_and_no_document_text(engine, acme):
    engine.pipeline_mode = "on"
    r = ask(engine, acme.biz, acme.owner, "Which plans include API access and have at least 5 seats?")
    plan, evidence, suff, verification = r["query_plan"], r["evidence"], r["sufficiency"], r["claim_verification"]
    assert plan["intent"] and "requirements" in plan and "conditions" in plan  # how the question was read
    assert evidence and all({"id", "entry_id", "status", "reasons", "tenant"} <= set(e) and e["id"].startswith("E") for e in evidence)  # retrieved, numbered, judged
    assert {e["status"] for e in evidence} <= {"accepted", "context", "rejected"} and all("content" not in e and "text" not in e for e in evidence)
    assert suff["status"] in ("SUFFICIENT", "PARTIAL", "CONFLICTED", "INSUFFICIENT", "AMBIGUOUS") and "needs_retrieval" in suff and "gap_queries" in suff
    assert verification["claims"] and all(c["status"] in ("verified", "removed", "not_factual") for c in verification["claims"])
    assert all("evidence_ids" in c for c in r["claims"])  # which evidence each claim rests on
    assert r["execution"]["mode"] and r["execution"]["generated_by"] and "fallback" in r["execution"]


# ---------------------------------------------------------------- parity, measured on the hosted data
def parity(engine, biz, owner, rows):
    engine.pipeline_mode = "shadow"
    engine.stats.clear()
    kept = []
    for row in rows:
        r = ask(engine, biz, owner, row["question"])
        assert r["execution"]["path"] == "legacy" and not (PIPELINE_KEYS & set(r))  # shadow returns ONE response: the existing one
        kept.append(r)
    return kept


def test_shadow_parity_on_the_acme_example(engine, acme, db):
    rows = rows_of(ROOT / "examples" / "unified" / "dev.csv") + rows_of(ROOT / "examples" / "unified" / "regression.csv")
    parity(engine, acme.biz, acme.owner, rows)
    recorded = audit(db, acme.biz, "pipeline.shadow_mismatch")
    assert engine.stats["shadow_match"] + engine.stats["shadow_mismatch"] == len(rows) and engine.stats["pipeline_error"] == 0
    assert engine.stats["shadow_mismatch"] == len(recorded)
    assert recorded == [], recorded  # not one difference in outcome, answer, claims, citations, conflicts, abstention, structured results, candidates or freshness


def test_shadow_parity_on_the_beacon_structured_questions(engine, beacon, db):
    rows = rows_of(REASONING / "dev.csv") + rows_of(REASONING / "regression.csv")
    parity(engine, beacon.biz, beacon.owner, rows)
    recorded = audit(db, beacon.biz, "pipeline.shadow_mismatch")
    assert engine.stats["shadow_match"] + engine.stats["shadow_mismatch"] == len(rows) and engine.stats["pipeline_error"] == 0
    assert recorded == [], recorded  # sums, counts, comparisons, minima and maxima: the same figures, the same rows cited


def test_both_paths_judge_the_example_sets_alike(engine, acme):
    rows = rows_of(ROOT / "examples" / "unified" / "dev.csv") + rows_of(ROOT / "examples" / "unified" / "regression.csv")
    engine.pipeline_mode = "off"
    old = [UB.judge(row, ask(engine, acme.biz, acme.owner, row["question"])) for row in rows]
    engine.pipeline_mode = "on"
    new = [UB.judge(row, ask(engine, acme.biz, acme.owner, row["question"])) for row in rows]
    assert [j["correct"] for j in old] == [j["correct"] for j in new]
    assert [j["outcome"] for j in old] == [j["outcome"] for j in new]
    assert sum(j["outdated_used"] for j in new) == 0
    assert engine.stats["pipeline_answered"] == len(rows)


def test_freshness_is_the_same_on_both_paths(db, engine, clock):
    owner = str(db.user("p-fresh"))
    biz = str(db.business(owner, "fresh-pipeline"))
    add(db, engine, biz, owner, "returns_2025.md", b"---\nvalid_until: 2026-03-31\n---\n# Returns\n\n## Return window\n\nItems can be returned within 14 days of delivery.\n")
    add(db, engine, biz, owner, "prices_v1.md", b"# Prices\n\n## Shipping\n\nShipping costs 5 pounds for every order.\n")
    add(db, engine, biz, owner, "prices_v2.md", b"---\nvalid_from: 2026-07-01\nsupersedes: prices_v1.md\n---\n# Prices\n\n## Shipping\n\nShipping costs 7 pounds for every order.\n")
    engine.pipeline_mode = "shadow"
    seen = []
    for day, q in ((date(2026, 3, 1), "Within how many days can items be returned?"), (date(2026, 6, 1), "Within how many days can items be returned?"), (date(2026, 6, 15), "How much does shipping cost?"),
                   (date(2026, 7, 2), "How much does shipping cost?")):
        clock["day"] = day
        seen.append(ask(engine, biz, owner, q))
    clock["day"] = DAY
    assert audit(db, biz, "pipeline.shadow_mismatch") == []
    assert "14 days" in seen[0]["answer"] and "14 days" not in (seen[1].get("answer") or "") and seen[1].get("outdated")  # expired: not used, and said
    assert "5 pounds" in seen[2]["answer"] and "7 pounds" in seen[3]["answer"] and "5 pounds" not in seen[3]["answer"]  # announced is not in force; replaced is not used
    engine.pipeline_mode = "on"
    clock["day"] = date(2026, 7, 2)
    r = ask(engine, biz, owner, "How much does shipping cost?")
    clock["day"] = DAY
    assert "7 pounds" in r["answer"] and r["sources"][0]["source"] == "prices_v2.md" and {e["tenant"] for e in r["evidence"]} <= {biz}


def test_a_difference_between_the_paths_is_recorded_without_text_and_never_returned(engine, acme, db, monkeypatch):
    real = svc.Pipeline.run

    def different(self, *a, **k):
        out = real(self, *a, **k)
        out["answer"] = "A DIFFERENT SECRET ANSWER"
        return out

    monkeypatch.setattr(svc.Pipeline, "run", different)
    engine.pipeline_mode = "shadow"
    r = ask(engine, acme.biz, acme.owner, "How long is the password reset link valid?")
    assert "DIFFERENT" not in json.dumps(r, default=str) and r["execution"]["path"] == "legacy"
    rec = audit(db, acme.biz, "pipeline.shadow_mismatch")
    assert engine.stats["shadow_mismatch"] == 1 and rec and "answer differs" in rec[-1]["differences"]
    assert "DIFFERENT" not in json.dumps(rec) and "password" not in json.dumps(rec).lower()  # phrases and ids; not the question, not the answer


def test_evidence_from_another_business_is_a_recorded_failure_of_parity(engine, acme, other, db, monkeypatch):
    real = svc.Pipeline.run

    def leaky(self, *a, **k):
        out = real(self, *a, **k)
        out["evidence"] = [{**e, "tenant": other.biz} for e in out["evidence"]]
        return out

    monkeypatch.setattr(svc.Pipeline, "run", leaky)
    engine.pipeline_mode = "shadow"
    ask(engine, acme.biz, acme.owner, "Does Acme Cloud have a SOC 2 report?")
    assert any("tenant context differs" in d for rec in audit(db, acme.biz, "pipeline.shadow_mismatch") for d in rec["differences"])


def test_the_pipeline_carries_the_business_through_every_stage(engine, acme, other):
    engine.pipeline_mode = "on"
    mine = ask(engine, acme.biz, acme.owner, "How much is the Pro plan?")
    assert mine["evidence"] and {e["tenant"] for e in mine["evidence"]} == {acme.biz}
    theirs = ask(engine, other.biz, other.owner, "How much is the Pro plan?")
    assert {e["tenant"] for e in theirs["evidence"]} <= {other.biz} and "Pro plan" not in json.dumps(theirs.get("sources") or [])
    names = {e["source"] for e in theirs["evidence"]} | {c["source"] for c in theirs["retrieval"]["candidates"]}
    assert names <= {"pets.csv"}  # nothing of the other business's knowledge, at any stage


# ---------------------------------------------------------------- the way back
def test_turning_the_flag_off_restores_the_existing_path_at_once(engine, acme):
    engine.pipeline_mode = "on"
    assert ask(engine, acme.biz, acme.owner, HOURS)["execution"]["path"] == "pipeline"
    engine.pipeline_mode = "off"
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert r["execution"]["path"] == "legacy" and not (PIPELINE_KEYS & set(r))


def test_a_fault_in_the_pipeline_never_fails_the_answer(engine, acme, db, monkeypatch, caplog):
    def broken(self, *a, **k):
        raise RuntimeError("boom: the secret handbook text and sk-live-123456")

    monkeypatch.setattr(svc.Pipeline, "run", broken)
    engine.pipeline_mode = "on"
    with caplog.at_level("INFO"):
        r = ask(engine, acme.biz, acme.owner, HOURS)
    assert r["outcome"] in ("faq", "grounded") and r["execution"]["path"] == "legacy" and r["execution"]["pipeline_note"] == "pipeline_error:RuntimeError"
    assert engine.stats["pipeline_error"] == 1 and engine.stats["legacy_answered"] == 1
    rec = audit(db, acme.biz, "pipeline.error")
    assert rec and rec[-1] == {"error": "pipeline_error:RuntimeError", "fell_back_to": "legacy"}
    assert "secret handbook" not in caplog.text and "sk-live" not in caplog.text and "secret handbook" not in json.dumps(rec)  # the fault is named, never its contents
    engine.pipeline_mode = "shadow"
    r = ask(engine, acme.biz, acme.owner, HOURS)  # in shadow the same fault is counted and the answer is unchanged
    assert r["outcome"] in ("faq", "grounded") and r["execution"]["path"] == "legacy" and engine.stats["pipeline_error"] == 2


def test_a_business_with_no_knowledge_is_answered_the_same_in_every_mode(engine, db):
    owner = str(db.user("p-empty"))
    biz = str(db.business(owner, "empty-pipeline"))
    for mode in ("off", "shadow", "on"):
        engine.pipeline_mode = mode
        r = ask(engine, biz, owner, "Anything?")
        assert "no knowledge" in r["message"] and r["execution"]["path"] == "legacy"


# ---------------------------------------------------------------- consent: who may read and change it
@pytest.fixture()
def version():
    return lambda provider: disclosure.current_version(provider)


def test_tenant_a_cannot_read_or_change_tenant_bs_consent(engine, acme, other, db, version):
    engine.consent_grant(other.biz, other.owner, "cloudflare", version("cloudflare"))
    for call in (lambda: engine.consent_get(other.biz, acme.owner),
                 lambda: engine.consent_grant(other.biz, acme.owner, "gemini", version("gemini")),
                 lambda: engine.consent_revoke(other.biz, acme.owner, "cloudflare"),
                 lambda: engine.disclosure_for(other.biz, acme.owner, "cloudflare")):
        with pytest.raises(svc.ServiceError) as e:
            call()
        assert e.value.status == 403
    assert next(p for p in engine.consent_get(other.biz, other.owner)["providers"] if p["provider"] == "cloudflare")["agreed"] is True  # untouched
    assert not any(p["agreed"] for p in engine.consent_get(acme.biz, acme.owner)["providers"])  # and A is not agreed to anything
    consent = svc.DbConsent(engine.store, acme.biz)
    assert consent.allows(acme.biz, "cloudflare") is False and consent.allows(other.biz, "cloudflare") is False  # a check for A cannot be answered by B's agreement, or asked about B
    db.su("update public.ai_inference_consents set status = 'revoked', revoked_at = now() where business_id = %s", (other.biz,))


def test_only_an_owner_or_admin_can_agree_and_nobody_can_agree_for_a_different_text(engine, acme, db, version):
    for who in (acme.editor, acme.viewer, str(db.user("p-stranger"))):
        with pytest.raises(svc.ServiceError) as e:
            engine.consent_grant(acme.biz, who, "cloudflare", version("cloudflare"))
        assert e.value.status == 403
    for call in (lambda: engine.consent_revoke(acme.biz, acme.editor, "cloudflare"), lambda: engine.consent_revoke(acme.biz, acme.viewer, "cloudflare")):
        with pytest.raises(svc.ServiceError) as e:
            call()
        assert e.value.status == 403
    assert engine.consent_get(acme.biz, acme.viewer)["providers"]  # everyone in the business can SEE where it stands
    with pytest.raises(svc.ServiceError) as e:
        engine.consent_grant(acme.biz, acme.owner, "cloudflare", "2020-01-01.deadbeef")
    assert e.value.status == 409
    with pytest.raises(svc.ServiceError) as e:
        engine.consent_grant(acme.biz, acme.owner, "not-a-provider", "x")
    assert e.value.status == 400
    assert db.su("select count(*) from public.ai_inference_consents where business_id = %s", (acme.biz,))[0][0] == 0  # none of it left a trace


def test_the_agreement_records_who_what_and_which_text_and_the_history_is_kept(engine, acme, db, version):
    v = version("cloudflare")
    state = engine.consent_grant(acme.biz, acme.owner, "cloudflare", v)
    row = next(p for p in state["providers"] if p["provider"] == "cloudflare")
    assert row["agreed"] is True and row["in_use"] is True and row["disclosure_version"] == v and row["terms_verified"] is True and row["consentable"] is True
    h = row["history"][0]
    assert h["status"] == "granted" and h["granted_by"] == acme.owner and h["disclosure_version"] == v and h["data_categories"] == ["question", "evidence_passages"]
    stored = db.su("select disclosure_sha256, granted_by::text from public.ai_inference_consents where business_id = %s and provider = 'cloudflare'", (acme.biz,))[0]
    assert stored == (disclosure.sha256("cloudflare"), acme.owner)  # the exact text shown is identifiable
    again = engine.consent_grant(acme.biz, acme.owner, "cloudflare", v)  # agreeing twice records nothing new
    assert len(next(p for p in again["providers"] if p["provider"] == "cloudflare")["history"]) == 1
    after = engine.consent_revoke(acme.biz, acme.owner, "cloudflare")
    h2 = next(p for p in after["providers"] if p["provider"] == "cloudflare")
    assert h2["agreed"] is False and h2["history"][0]["status"] == "revoked" and h2["history"][0]["revoked_by"] == acme.owner and h2["history"][0]["revoked_at"]
    trail = db.su("select actor_id::text, action, detail ->> 'provider', detail ->> 'status_after', detail::text from public.audit_logs where business_id = %s and action like 'ai_inference_consents.%%' order by created_at",
                  (acme.biz,))
    assert [t[1] for t in trail] == ["ai_inference_consents.insert", "ai_inference_consents.update"] and {t[0] for t in trail} == {acme.owner} and trail[1][3] == "revoked"
    assert not any(w in "".join(t[4] for t in trail) for w in ("question", "answer", "password"))


# ---------------------------------------------------------------- consent: nothing leaves without it
def faithful(system, user):
    """A model that restates the first numbered evidence item, exactly (a valid answer)."""
    m = re.search(r"\[(E\d+)\] \([^)]*\) (.*)", user)
    return reply_json("answered", [(re.split(r"(?<=[.!?])\s", m.group(2).strip())[0], [m.group(1)])])


def serve_with(engine, provider):
    engine.provider_factory = lambda biz: provider
    engine.pipeline_mode = "on"


def test_without_consent_no_text_reaches_a_provider(engine, acme):
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert p.calls == []  # the provider was never called
    ex = r["execution"]
    assert ex["generated_by"] == "deterministic_no_consent" and ex["failure_kind"] == "consent:missing_or_revoked" and ex["inference_attempted"] is False and ex["inference_succeeded"] is False
    assert r["outcome"] in ("faq", "grounded") and r["answer"]  # and the customer is still answered, by the deterministic engine


def test_with_consent_the_provider_is_used_and_the_answer_says_a_model_wrote_it(engine, acme, version):
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    r = ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")
    assert len(p.calls) == 1 and r["execution"]["generated_by"] == "model" and r["execution"]["mode"] == "platform" and r["execution"]["provider"] == "cloudflare"
    assert r["execution"]["inference_attempted"] is True and r["execution"]["inference_succeeded"] is True and r["execution"]["validation"] in ("verified", "partly_verified")
    assert "api_key" not in p.calls[0] and acme.owner not in p.calls[0]  # the prompt holds the question and evidence passages, not a credential and not a person's id
    engine.consent_revoke(acme.biz, acme.owner, "cloudflare")


def test_revoking_blocks_the_very_next_request(engine, acme, version):
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    assert ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")["execution"]["generated_by"] == "model"
    used = len(p.calls)
    engine.consent_revoke(acme.biz, acme.owner, "cloudflare")
    r = ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")
    assert len(p.calls) == used and r["execution"]["generated_by"] == "deterministic_no_consent" and r["execution"]["failure_kind"] == "consent:missing_or_revoked"
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))  # agreeing again is a new agreement, and works again
    assert ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")["execution"]["generated_by"] == "model"
    engine.consent_revoke(acme.biz, acme.owner, "cloudflare")


def test_consent_for_one_provider_does_not_authorise_another(engine, acme, version):
    engine.consent_grant(acme.biz, acme.owner, "gemini", version("gemini"))
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert p.calls == [] and r["execution"]["generated_by"] == "deterministic_no_consent"
    g = MockProvider([faithful], name="gemini")
    serve_with(engine, g)
    assert ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")["execution"]["generated_by"] == "model" and len(g.calls) == 1
    engine.consent_revoke(acme.biz, acme.owner, "gemini")


def test_one_business_agreeing_does_not_open_another_business_to_the_provider(engine, acme, other, version):
    engine.consent_grant(other.biz, other.owner, "cloudflare", version("cloudflare"))
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    ask(engine, acme.biz, acme.owner, HOURS)
    assert p.calls == []  # A never agreed
    assert len(ask(engine, other.biz, other.owner, "Do you groom cats?")["execution"]["generated_by"]) and len(p.calls) == 1
    assert "Pro plan" not in p.calls[0] and "Acme" not in p.calls[0]  # and what was sent for B is B's
    engine.consent_revoke(other.biz, other.owner, "cloudflare")


def test_a_changed_disclosure_text_ends_the_earlier_agreement(engine, acme, version, monkeypatch):
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    original = disclosure.COMMON["third_party"]
    monkeypatch.setitem(disclosure.COMMON, "third_party", original + " The terms were revised.")
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert p.calls == [] and r["execution"]["generated_by"] == "deterministic_no_consent"  # agreed to the text that was shown, not to the one that replaced it
    assert next(x for x in engine.consent_get(acme.biz, acme.owner)["providers"] if x["provider"] == "cloudflare")["agreed"] is False
    with pytest.raises(svc.ServiceError) as e:
        engine.consent_grant(acme.biz, acme.owner, "cloudflare", disclosure.current_version("cloudflare").replace(disclosure.BASE_VERSION, "2020-01-01"))
    assert e.value.status == 409
    monkeypatch.setitem(disclosure.COMMON, "third_party", original)
    assert next(x for x in engine.consent_get(acme.biz, acme.owner)["providers"] if x["provider"] == "cloudflare")["agreed"] is True  # the old text, the old agreement
    engine.consent_revoke(acme.biz, acme.owner, "cloudflare")


def test_a_provider_that_fails_neither_bypasses_consent_nor_changes_it(engine, acme, version):
    failing = MockProvider([], name="cloudflare")  # no reply scripted: every call is a provider failure
    serve_with(engine, failing)
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert failing.calls == [] and r["execution"]["generated_by"] == "deterministic_no_consent"  # failure or not, it is not called without consent
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    r = ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")
    ex = r["execution"]
    assert len(failing.calls) == 1 and ex["generated_by"] == "deterministic_fallback" and ex["failure_kind"].startswith("provider:") and ex["fallback"] is True  # a PROVIDER failure, said as one
    assert r["outcome"] in ("faq", "grounded") and r["answer"] and ex["inference_succeeded"] is False  # the customer still has the engine's own answer, and it is not called a model's
    assert next(x for x in engine.consent_get(acme.biz, acme.owner)["providers"] if x["provider"] == "cloudflare")["agreed"] is True  # a failure does not touch the agreement either way
    engine.consent_revoke(acme.biz, acme.owner, "cloudflare")
    ask(engine, acme.biz, acme.owner, HOURS)
    assert len(failing.calls) == 1  # and after revoking, not even a retry


def test_a_model_whose_answer_cannot_be_used_is_a_reasoning_failure_not_a_provider_failure(engine, acme, version):
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    garbage = MockProvider([lambda s, u: __import__("faqengine.runtime.contracts", fromlist=["ProviderReply"]).ProviderReply(text="Sure, happy to help!", tokens_in=10, tokens_out=4, seconds=0.01)], name="cloudflare")
    serve_with(engine, garbage)
    ex = ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")["execution"]
    assert ex["failure_kind"].startswith("reasoning:") and ex["generated_by"] == "deterministic_fallback" and ex["inference_attempted"] is True
    engine.consent_revoke(acme.biz, acme.owner, "cloudflare")


def test_a_failed_provider_is_never_replaced_by_another_one(engine, acme, version):
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    engine.consent_grant(acme.biz, acme.owner, "gemini", version("gemini"))
    first, second = MockProvider([], name="cloudflare"), MockProvider([faithful], name="gemini")
    asked = []
    engine.provider_factory = lambda biz: (asked.append(biz), first)[1]
    engine.pipeline_mode = "on"
    r = ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")
    assert len(first.calls) == 1 and second.calls == [] and asked == [acme.biz]  # no hidden retry elsewhere, even where the business agreed to the other provider
    assert r["execution"]["generated_by"] == "deterministic_fallback" and r["execution"]["provider"] == "cloudflare"
    for pr in ("cloudflare", "gemini"):
        engine.consent_revoke(acme.biz, acme.owner, pr)


def test_deterministic_mode_needs_no_consent_no_credentials_and_no_provider(db, engine, acme, other, monkeypatch):
    asked = []
    monkeypatch.setattr(engine.store, "consent_allows", lambda *a: asked.append(a) or False)
    bare = svc.HostedEngine(engine.store, engine.embedder, None, None, rate_per_minute=100_000, today=engine.today, pipeline_mode="on")  # no secret box, no storage, no provider
    r = ask(bare, other.biz, other.owner, "Do you groom cats?")
    ex = r["execution"]
    assert ex["path"] == "pipeline" and ex["generated_by"] == "deterministic" and ex["mode"] == "deterministic" and ex["provider"] is None and ex["inference_attempted"] is False
    assert r["outcome"] in ("faq", "grounded", "insufficient", "clarification")
    assert asked == []  # consent was not even consulted: nothing could have been sent, so there is nothing to ask
    assert bare.provider_factory is None and bare.stats["pipeline_answered"] == 1


def test_a_request_cannot_choose_a_provider_or_claim_consent(engine, acme, db):
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), svc.make_handler(engine, "shared-secret-for-tests-0123456789"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        def call(path, body):
            req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_port}{path}", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer shared-secret-for-tests-0123456789"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        body = {"business_id": acme.biz, "user_id": acme.viewer, "query": HOURS, "provider": "cloudflare", "consent": True, "mode": "platform", "consented": True, "pipeline": "on", "disclosure_version": "x"}
        status, data = call("/v1/respond", body)
        assert status == 200 and p.calls == [] and data["result"]["execution"]["generated_by"] == "deterministic_no_consent"  # fields in the body are ignored: consent is read from the database
        assert call("/v1/ai/consent/grant", {"business_id": acme.biz, "user_id": acme.viewer, "provider": "cloudflare", "disclosure_version": disclosure.current_version("cloudflare")})[0] == 403
        assert call("/v1/ai/consent/get", {"business_id": acme.biz, "user_id": acme.viewer})[0] == 200
        status, data = call("/v1/ai/disclosure", {"business_id": acme.biz, "user_id": acme.viewer, "provider": "cloudflare"})
        assert status == 200 and data["consentable"] is True and data["version"] == disclosure.current_version("cloudflare") and "Terms change" in data["text"]
        status, data = call("/v1/ai/consent/grant", {"business_id": acme.biz, "user_id": acme.owner, "provider": "cloudflare", "disclosure_version": disclosure.current_version("cloudflare")})
        assert status == 200 and next(x for x in data["providers"] if x["provider"] == "cloudflare")["agreed"] is True
        assert call("/v1/ai/consent/revoke", {"business_id": acme.biz, "user_id": acme.owner, "provider": "cloudflare"})[0] == 200
        assert call("/v1/ai/consent/get", {"business_id": acme.biz, "user_id": acme.owner})[1]["external_inference"]["connected"] is False
        assert call("/health", {})[1] is not None
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------- the business's own model is gated by the same consent
@pytest.fixture()
def own_gemini(db, engine, acme):
    """A business that saved its own Gemini key and chose its own provider, but has agreed to nothing."""
    db.su("insert into public.llm_provider_configs (business_id, provider, model, enabled, has_key) values (%s, 'gemini', 'g-1', true, true)", (acme.biz,))
    row = engine.secrets.encrypt("sk-never-decrypted-1234", acme.biz)
    db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key, key_version) values (%s, %s, %s)", (acme.biz, row[0], row[1]))
    engine.ai_settings_save(acme.biz, acme.owner, {"mode": "byok"})
    yield
    engine.store.save_ai_settings(acme.biz, acme.owner, "deterministic", None, None, "deterministic")
    db.su("delete from public.llm_provider_secrets where business_id = %s", (acme.biz,))
    db.su("delete from public.llm_provider_configs where business_id = %s", (acme.biz,))


def test_a_business_model_chosen_but_not_agreed_to_is_left_unused_and_its_key_is_not_even_decrypted(engine, acme, own_gemini, monkeypatch):
    opened = []
    monkeypatch.setattr(engine.secrets, "decrypt", lambda *a, **k: opened.append(a) or "sk-never-decrypted-1234")
    r = engine.router.route(acme.biz)
    assert r.mode == "byok" and r.refusal == "consent:missing_or_revoked" and r.provider == "gemini" and opened == []  # a key for a provider that was not agreed to is never opened
    ex = ask(engine, acme.biz, acme.owner, HOURS)["execution"]
    assert ex["generated_by"] == "deterministic_no_consent" and ex["provider"] == "gemini" and ex["failure_kind"] == "consent:missing_or_revoked" and ex["inference_attempted"] is False and opened == []


def test_the_business_model_page_says_when_a_saved_model_is_not_in_use(engine, acme, own_gemini, unverified, db):
    state = engine.llm_public(acme.biz, acme.viewer)
    assert state["enabled"] is True and state["in_use"]["used"] is False and state["in_use"]["reason"] == "provider_unverified" and "answers stay local" in state["in_use"]["message"]
    ex = ask(engine, acme.biz, acme.owner, HOURS)["execution"]
    assert ex["generated_by"] == "deterministic_no_consent" and ex["failure_kind"] == "consent:provider_unverified"
    engine.store.save_ai_settings(acme.biz, acme.owner, "deterministic", None, None, "deterministic")
    assert engine.llm_public(acme.biz, acme.viewer)["in_use"]["reason"] == "mode_is_deterministic"  # saving a model does not choose the mode


# ---------------------------------------------------------------- a provider whose terms are not verified can be neither agreed to nor used
@pytest.fixture()
def unverified(monkeypatch):
    """The real entries: every provider unverified."""
    monkeypatch.delenv("AI_DISCLOSURE_FILE", raising=False)


def test_an_unverified_provider_cannot_be_agreed_to_and_the_list_says_why(engine, acme, db, unverified):
    for who in (acme.owner,):
        with pytest.raises(svc.ServiceError) as e:
            engine.consent_grant(acme.biz, who, "cloudflare", disclosure.current_version("cloudflare"))
        assert e.value.status == 409 and "not been verified" in e.value.message
    assert db.su("select count(*) from public.ai_inference_consents where business_id = %s and provider = 'cloudflare' and status = 'granted'", (acme.biz,))[0][0] == 0
    row = next(p for p in engine.consent_get(acme.biz, acme.viewer)["providers"] if p["provider"] == "cloudflare")
    assert row["consentable"] is False and row["terms_verified"] is False and row["agreed"] is False and "how long the provider keeps submitted text" in " ".join(
        disclosure.LABELS[k] for k in row["unverified"])
    d = engine.disclosure_for(acme.biz, acme.viewer, "cloudflare")
    assert d["consentable"] is False and "Agreement is not available for this provider" in d["text"]  # the text is readable; the button does not exist


def test_an_agreement_does_not_outlive_the_verification_it_was_made_under(engine, acme, db, version, monkeypatch, tmp_path):
    engine.consent_grant(acme.biz, acme.owner, "cloudflare", version("cloudflare"))
    p = MockProvider([faithful], name="cloudflare")
    serve_with(engine, p)
    assert ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")["execution"]["generated_by"] == "model"
    used = len(p.calls)
    lapsed = json.loads(open(__import__("os").environ["AI_DISCLOSURE_FILE"]).read())
    lapsed["cloudflare"]["retention"] = "unknown"  # the entry is edited: what was verified no longer is
    f = tmp_path / "lapsed.json"
    f.write_text(json.dumps(lapsed))
    monkeypatch.setenv("AI_DISCLOSURE_FILE", str(f))
    r = ask(engine, acme.biz, acme.owner, "Which web browsers does the app work in?")
    assert len(p.calls) == used and r["execution"]["generated_by"] == "deterministic_no_consent"
    row = next(x for x in engine.consent_get(acme.biz, acme.owner)["providers"] if x["provider"] == "cloudflare")
    assert row["in_use"] is False and row["consentable"] is False
    after = engine.consent_revoke(acme.biz, acme.owner, "cloudflare")  # a withdrawal is always possible
    assert not next(x for x in after["providers"] if x["provider"] == "cloudflare")["agreed"]


def test_a_stale_agreement_row_for_an_unverified_provider_authorises_nothing(engine, acme, db, unverified):
    v = disclosure.current_version("cloudflare")
    db.su("insert into public.ai_inference_consents (business_id, provider, disclosure_version, disclosure_sha256, granted_by) values (%s, 'cloudflare', %s, %s, %s)",
          (acme.biz, v, disclosure.sha256("cloudflare"), acme.owner))  # (as if it had been written by some other route)
    try:
        p = MockProvider([faithful], name="cloudflare")
        serve_with(engine, p)
        r = ask(engine, acme.biz, acme.owner, HOURS)
        assert p.calls == [] and r["execution"]["generated_by"] == "deterministic_no_consent"
        assert next(x for x in engine.consent_get(acme.biz, acme.owner)["providers"] if x["provider"] == "cloudflare")["in_use"] is False
    finally:
        engine.consent_revoke(acme.biz, acme.owner, "cloudflare")


# ---------------------------------------------------------------- the controlled shadow test: sampling, kinds, allowance, metrics
def test_the_sample_chooses_how_many_requests_also_run_the_pipeline(engine, acme):
    engine.pipeline_mode = "shadow"
    engine.shadow_sample, engine._rng = 0.0, lambda: 0.5
    try:
        for _ in range(3):
            ask(engine, acme.biz, acme.owner, HOURS)
        assert engine.stats["shadow_sampled_out"] == 3 and engine.stats["shadow_match"] == 0 and not engine.latency["pipeline"]  # nothing ran beside the answers
        engine.shadow_sample = 0.5
        ask(engine, acme.biz, acme.owner, HOURS)  # 0.5 >= 0.5: out
        engine._rng = lambda: 0.49
        ask(engine, acme.biz, acme.owner, HOURS)  # in
        assert engine.stats["shadow_sampled_out"] == 4 and engine.stats["shadow_match"] == 1 and len(engine.latency["pipeline"]) == 1
    finally:
        engine.shadow_sample, engine._rng = 1.0, __import__("random").random
    with pytest.raises(ValueError):
        svc.HostedEngine(None, None, shadow_sample=1.5)
    with pytest.raises(SystemExit, match="ENGINE_SHADOW_SAMPLE"):
        svc.from_env({"DATABASE_URL": "x", "ENGINE_SERVICE_SECRET": "s" * 30, "EMBEDDING_MODEL_DIR": str(MODEL), "ENGINE_SHADOW_SAMPLE": "most"})
    with pytest.raises(SystemExit, match="ENGINE_SHADOW_SAMPLE"):
        svc.from_env({"DATABASE_URL": "x", "ENGINE_SERVICE_SECRET": "s" * 30, "EMBEDDING_MODEL_DIR": str(MODEL), "ENGINE_SHADOW_SAMPLE": "2"})


def test_metrics_report_counts_kinds_and_the_median_and_p95_of_both_paths(engine, acme):
    engine.pipeline_mode = "shadow"
    for q in (HOURS, "Which web browsers does the app work in?", "How long is the password reset link valid?"):
        ask(engine, acme.biz, acme.owner, q)
    m = engine.metrics()
    assert m["mode"] == "shadow" and m["shadow_sample"] == 1.0 and m["shadow_match"] == 3 and m["legacy_answered"] == 3
    for path in ("legacy", "pipeline"):
        lat = m["latency"][path]
        assert lat["n"] == 3 and 0 < lat["median_ms"] <= lat["p95_ms"]
    assert not any(k in json.dumps(m) for k in (acme.owner, acme.editor, acme.viewer, "password", "reset link"))  # counts and times: no person and no text (the busiest businesses are listed by id, for the operator)


def test_a_difference_is_recorded_with_its_kinds_and_counted_by_kind(engine, acme, db, monkeypatch):
    real = svc.Pipeline.run

    def different(self, *a, **k):
        out = real(self, *a, **k)
        out["answer"] = "something else"
        out["sources"] = []
        out["evidence"] = out["evidence"][:1]
        return out

    monkeypatch.setattr(svc.Pipeline, "run", different)
    engine.pipeline_mode = "shadow"
    ask(engine, acme.biz, acme.owner, "How long is the password reset link valid?")
    rec = audit(db, acme.biz, "pipeline.shadow_mismatch")[-1]
    assert {"answer", "citations"} <= set(rec["categories"]) and rec["pipeline_ms"] and "question" not in json.dumps(rec).lower()
    assert engine.stats["shadow_diff_answer"] == 1 and engine.stats["shadow_diff_citations"] == 1


def test_a_burst_of_differences_cannot_become_a_burst_of_writes(engine, acme, db, monkeypatch):
    real = svc.Pipeline.run
    monkeypatch.setattr(svc.Pipeline, "run", lambda self, *a, **k: {**real(self, *a, **k), "answer": "always different"})
    engine.pipeline_mode = "shadow"
    before = len(audit(db, acme.biz, "pipeline.shadow_mismatch"))
    engine.shadow_records_per_hour, engine._recorded = 2, {}
    try:
        for _ in range(5):
            ask(engine, acme.biz, acme.owner, HOURS)
    finally:
        engine.shadow_records_per_hour, engine._recorded = 50, {}
    assert len(audit(db, acme.biz, "pipeline.shadow_mismatch")) - before == 2 and engine.stats["shadow_mismatch"] == 5 and engine.stats["shadow_unrecorded"] == 3  # all counted, two written


def test_a_pipeline_fault_in_shadow_is_a_shadow_exception_and_counts_as_a_fallback_of_the_pipeline_not_of_the_answer(engine, acme, monkeypatch):
    monkeypatch.setattr(svc.Pipeline, "run", lambda self, *a, **k: (_ for _ in ()).throw(KeyError("x")))
    engine.pipeline_mode = "shadow"
    r = ask(engine, acme.biz, acme.owner, HOURS)
    assert r["outcome"] in ("faq", "grounded") and engine.stats["shadow_error"] == 1 and engine.stats["pipeline_error"] == 1 and r["execution"]["pipeline_note"] == "pipeline_error:KeyError"
