"""Phase H, the Studio: the reasoning pipeline behind the same setting and the same code as the hosted service, with no separate reasoning of its own.

The engine tests need a trained run of the evidence example (runs/evidence-kb; built once with FAQENGINE_SLOW=1, then reused). They ask only the dev and regression sets, never the locked one.
The wiring tests need nothing trained."""
import csv
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
RUN = ROOT / "runs" / "evidence-kb"
EVID = ROOT / "examples" / "evidence"

from faqengine.runtime import pipeline as P  # noqa: E402
from faqengine.runtime import shadow  # noqa: E402
from faqengine.studio import server  # noqa: E402
from faqengine.studio.engine import ProjectEngine  # noqa: E402
from faqengine.studio.store import read_json  # noqa: E402


@pytest.fixture(scope="module")
def eng():
    if not (RUN / "report.json").exists():
        if not os.environ.get("FAQENGINE_SLOW"):
            pytest.skip("needs the trained evidence run (runs/evidence-kb): FAQENGINE_SLOW=1 builds it once")
        import evidence_benchmark as EB

        EB.build_run(RUN)
    return ProjectEngine(RUN)


@pytest.fixture(scope="module")
def th():
    return read_json(RUN / "thresholds.json") or {"floor": 0.4}


def questions(*names):
    return [r["question"] for n in names for r in csv.DictReader(open(EVID / f"{n}.csv", encoding="utf-8"))]


# ---------------------------------------------------------------- the same pipeline, measured
def test_the_studio_pipeline_agrees_with_the_existing_path_on_every_dev_and_regression_question(eng, th):
    asked = questions("dev", "regression")
    seen = []
    for q in asked:
        r = eng.respond(q, th, pipeline="shadow")
        x = r["execution"]
        assert x["path"] == "legacy" and x["shadow"]["compared"] is True and not (set(r) & {"query_plan", "evidence", "sufficiency", "claim_verification"})  # one response: the existing one
        if x["shadow"]["differences"]:
            seen.append((q, x["shadow"]["differences"]))
    assert seen == [], seen


def test_on_answers_with_the_pipeline_and_shows_every_stage(eng, th):
    r = eng.respond("How long does the grooming take and do you offer nail trimming?", th, pipeline="on")
    x = r["execution"]
    assert x["path"] == "pipeline" and x["mode"] == "deterministic" and x["generated_by"] == "deterministic" and x["provider"] is None and x["inference_attempted"] is False
    assert r["query_plan"]["requirements"] and r["evidence"] and r["sufficiency"]["status"] and r["claim_verification"]["claims"] is not None
    assert {"understand", "validate", "compose", "verify", "total"} <= set(x["stage_ms"]) and x["retrieval_passes"] in (1, 2)
    assert all("content" not in e for e in r["evidence"])  # ids, sources and judgements; never the document text
    off = eng.respond("How long does the grooming take and do you offer nail trimming?", th)
    assert off["outcome"] == r["outcome"] and off["execution"]["path"] == "legacy" and "query_plan" not in off


def test_a_wrong_setting_is_an_error_not_a_silent_off(eng, th):
    with pytest.raises(ValueError, match="pipeline setting"):
        eng.respond("hello", th, pipeline="yes")


# ---------------------------------------------------------------- the way back, and what it cannot do
def test_a_fault_in_the_pipeline_falls_back_to_the_existing_answer(eng, th, monkeypatch):
    def broken(self, *a, **k):
        raise RuntimeError("secret document text")

    monkeypatch.setattr(P.Pipeline, "run", broken)
    q = questions("dev")[0]
    for mode in ("on", "shadow"):
        r = eng.respond(q, th, pipeline=mode)
        assert r["outcome"] and r["execution"]["path"] == "legacy" and r["execution"]["pipeline_note"] == "pipeline_error:RuntimeError" and "secret" not in str(r["execution"])


def test_the_studio_pipeline_is_given_no_provider_and_no_consent_so_it_cannot_send_anything(eng, th, monkeypatch):
    made = []
    real = P.Pipeline.__init__

    def spy(self, *a, **k):
        made.append((a, k))
        real(self, *a, **k)

    monkeypatch.setattr(P.Pipeline, "__init__", spy)
    for mode in ("shadow", "on"):
        eng.respond(questions("dev")[1], th, pipeline=mode)
    assert made and all(a == ("deterministic",) and k == {} for a, k in made)  # deterministic, nothing else: no provider, no consent policy, no composer
    params = inspect.signature(ProjectEngine.respond).parameters
    assert not {"provider", "consent", "composer", "mode"} & set(params)  # and nothing in the request can add one


def test_a_project_with_its_own_model_on_keeps_the_existing_path_and_says_so(eng, th, monkeypatch):
    made = []
    monkeypatch.setattr(P.Pipeline, "__init__", lambda self, *a, **k: made.append(1))

    class OwnModel:  # a project's own language model, as the Studio connects it
        cfg = types.SimpleNamespace(provider="openai_compatible", model="m")

        def can(self, what):
            return False

        def usage(self):
            return {"calls": 0, "tokens_in": 0, "tokens_out": 0, "seconds": 0}

    r = eng.respond(questions("dev")[0], th, llm=OwnModel(), pipeline="on")
    assert made == [] and r["execution"]["path"] == "legacy" and "own language model" in r["execution"]["pipeline_note"]


# ---------------------------------------------------------------- the website around it
def test_the_studio_setting_comes_from_the_environment_and_a_typo_stops_it(tmp_path, monkeypatch):
    from faqengine.studio.store import Store

    monkeypatch.delenv("FAQENGINE_PIPELINE", raising=False)
    assert server.App(Store(tmp_path)).pipeline == "off"
    monkeypatch.setenv("FAQENGINE_PIPELINE", "Shadow")
    assert server.App(Store(tmp_path)).pipeline == "shadow"
    monkeypatch.setenv("FAQENGINE_PIPELINE", "sometimes")
    with pytest.raises(ValueError, match="pipeline setting"):
        server.App(Store(tmp_path))
    assert shadow.mode_from("") == "off" and shadow.mode_from(None) == "off"


def test_the_test_route_passes_the_studios_setting_and_nothing_from_the_request(tmp_path):
    from faqengine.studio.store import Store

    calls = []

    class Stub:
        def ask(self, q, th):
            return {"band": "llm", "confidence": 0.9, "hits": [{"id": "e1", "score": 0.9}], "latency_ms": 1.0, "point": None}

        def respond(self, q, th, llm=None, **k):
            calls.append((llm, k))
            return {"outcome": "faq", "answer": "x", "execution": {"path": "legacy"}}

    app = server.App(Store(tmp_path), pipeline="shadow")
    app.store = types.SimpleNamespace(status=lambda pid: {"state": "trained"}, thresholds=lambda pid: {"floor": 0.4}, add_test=lambda pid, d: "t1", meta=lambda pid: {}, dir=lambda pid: tmp_path)
    app.engine = lambda pid: Stub()
    app.llm = lambda pid: types.SimpleNamespace(connection=lambda: None)
    out = app.test("p1", "hello")
    assert calls == [(None, {"pipeline": "shadow"})] and out["response"]["execution"]["path"] == "legacy"  # the setting is the server's; the request carries a question and nothing else
    assert list(inspect.signature(server.App.test).parameters) == ["self", "pid", "query"]
    assert 'app.test(pid, str(self._body().get("query") or ""))' in inspect.getsource(server)  # the route hands over the question alone


def test_the_page_has_the_how_this_was_decided_view_with_every_stage():
    js = (ROOT / "src" / "faqengine" / "studio" / "static" / "app.js").read_text(encoding="utf-8")
    for needle in ("How this was decided", "How the question was read", "Evidence considered", "Was the evidence enough?", "Targeted retrieval", "Claim by claim", "Written by", "Fallback reason",
                   "Compared with the existing path", "Model called", "deterministic_no_consent", "cited as"):
        assert needle in js, needle
