"""The model benchmark's scoring (scripts/llm_bench.py): a model is judged by the engine's own judge, plus ids it invents, numbers in no cited evidence and invalid JSON; a provider's failure is not the
model's. Uses frozen evidence and a fake model: no network, no model."""
import json
import pytest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))
import llm_bench as B  # noqa: E402

ROW = {"id": "t", "category": "x", "source_types": "csv", "question": "How much is the Growth plan?", "expected": "answered", "must_contain": "79 USD", "must_not_contain": "89",
       "cite": "plans.csv", "forbid_cite": "", "gold": "plans.csv"}
ITEM = {"row": ROW, "evidence": [{"id": "E1", "entry_id": "plans-003", "source": "plans.csv", "heading": "Growth", "text": "Growth plan, monthly price: 79 USD.", "kind": "passage"},
                                 {"id": "E2", "entry_id": "faq-1", "source": "faq.csv", "heading": "Trial", "text": "No trial.", "kind": "faq"}]}


def reply(**d):
    return B.Reply(text=json.dumps({"requirements": ["price"], "verdict": "answered", "missing": [], "answer": "The Growth plan costs 79 USD [E1].", "claims": [{"text": "79 USD", "evidence": ["E1"]}], **d}))


def test_a_grounded_cited_answer_is_correct():
    j = B.judge_model(ITEM, reply())
    assert j["correct"] and j["cite_ok"] and j["invented_ids"] == 0 and j["ungrounded_numbers"] == 0 and j["schema_ok"]


def test_an_invented_evidence_id_and_a_number_from_nowhere_are_counted():
    j = B.judge_model(ITEM, reply(answer="The Growth plan costs 79 USD [E1] and 120 USD [E9].", claims=[{"text": "120", "evidence": ["E9"]}]))
    assert j["invented_ids"] == 1 and j["ungrounded_numbers"] == 1 and j["uncited_claims"] == 1  # 120 is in no cited evidence


def test_invalid_json_is_a_model_failure_and_a_wrong_scope_is_not_cited_evidence():
    j = B.judge_model(ITEM, B.Reply(text="Sure! The answer is 79 USD."))
    assert j["model_failure"] == "invalid_json" and not j["correct"]
    assert B.parse_json("```json\n{\"a\": 1}\n```") == {"a": 1} and B.parse_json("<think>hm</think>{\"a\": 2}") == {"a": 2}


def test_a_refusal_is_scored_as_an_abstention():
    row = {**ROW, "expected": "insufficient", "must_contain": "", "cite": "", "gold": ""}
    j = B.judge_model({**ITEM, "row": row}, reply(verdict="insufficient", answer="", claims=[], missing=["price"]))
    assert j["abstained"] and j["correct"]


def test_the_prompt_numbers_evidence_with_immutable_ids():
    p = B.prompt(ITEM)
    assert "[E1] (plans.csv › Growth)" in p and "[E2]" in p


# ---- the production adapters, the seventeen measures, and the sealed holdout
import csv  # noqa: E402
import hashlib  # noqa: E402
import subprocess  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from fake_provider import FakeProvider  # noqa: E402

SEVENTEEN = ["1_overall_correctness", "2_false_answer_rate", "3_citation_correctness", "4_abstention_precision", "5_abstention_recall", "6_unnecessary_abstention", "7_conflict_detection",
             "8_missing_information_handling", "9_multi_source_reasoning", "10_structured_reasoning", "11_invalid_json_rate", "12_fabricated_evidence_id_rate", "13_provider_failure_rate",
             "14_median_latency_s", "15_p95_latency_s", "16_cost_per_question_usd", "17_cost_per_1000_questions_usd"]


def frozen(tmp_path, items, set_name="fresh"):
    f = tmp_path / "evidence.json"
    f.write_text(json.dumps({"data": "x", "set": set_name, "as_of": "2026-06-01", "items": items}))
    return f


def args(**kw):
    import argparse
    return argparse.Namespace(**{"deterministic": False, "provider": "llamacpp", "model": "m", "base_url": None, "mode": "none", "extra": None, "timeout": 20.0, "retries": 0, "limit": None, "price_in": 1.0,
                                 "price_out": 2.0, "json": None, "resume": False, "final": False, **kw})


def test_a_hosted_or_local_provider_is_called_through_the_production_adapter():
    p = FakeProvider(key="sk-bench-1234567", reply=lambda body: json.dumps({"requirements": ["price"], "verdict": "answered", "missing": [], "answer": "79 USD [E1]", "claims": [{"text": "79 USD", "evidence": ["E1"]}]}))
    try:
        prov = B.Production("llamacpp", "chat-small", "local", key="sk-bench-1234567", base_url=p.url(), timeout=10, structured="none")
        r = prov.complete(B.SYSTEM, B.prompt(ITEM), B.SCHEMA)
        assert r.failure is None and "79 USD" in r.text and r.tokens_in == 11 and r.tokens_out == 7
        assert [x for x in p.requests if x["path"].endswith("/chat/completions")]  # it went through the production request building
        p.status = 429
        assert prov.complete("s", "u", None).failure == "provider:rate_limited"  # a provider's failure keeps the production vocabulary, so the harness counts it apart from the model
    finally:
        p.close()


def test_the_credentials_the_benchmark_reads_are_its_own_names_and_never_the_service_s(monkeypatch):
    for name in ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "OPENROUTER_API_KEY", "PLATFORM_OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PLATFORM_OPENROUTER_API_KEY", "service-key-should-not-be-used")
    with pytest.raises(SystemExit, match="OPENROUTER_API_KEY"):
        B.make_provider(args(provider="openrouter", model="x"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "bench-key-0123456789")
    prov = B.make_provider(args(provider="openrouter", model="x", mode="schema"))
    assert isinstance(prov, B.Production) and prov.adapter._key == "bench-key-0123456789"


def test_every_one_of_the_seventeen_measures_is_reported():
    rows = [{**ROW, "id": f"q{i}", "category": c, "expected": e} for i, (c, e) in enumerate([("structured", "answered"), ("multi_source", "answered"), ("missing", "insufficient"), ("conflict", "conflict")])]
    items = [{"row": r, "evidence": ITEM["evidence"], "deterministic": {"outcome": "faq", "answer": "x", "sources": [], "conflicts": [], "candidates": []}} for r in rows]
    judged = [B.judge_model(it, B.Reply(text=json.dumps({"requirements": [], "verdict": "answered", "missing": [], "answer": "79 USD [E1]", "claims": [{"text": "79 USD", "evidence": ["E1"]}]}))) for it in items]
    m = B.seventeen({"correct_answer_rate": 0.5, "false_answer_rate": 0.25}, judged, rows, [1.0, 2.0, 3.0, 4.0], 5, {"provider:timeout": 1}, 0.02, 4)
    assert [k for k in m if k != "note"] == SEVENTEEN
    assert m["13_provider_failure_rate"] == 0.2 and m["15_p95_latency_s"] == 4.0 and m["14_median_latency_s"] == 3.0 and m["17_cost_per_1000_questions_usd"] == 5.0
    assert m["11_invalid_json_rate"] == 0.0 and m["12_fabricated_evidence_id_rate"] == 0.0
    no_cost = B.seventeen({}, judged, rows, [], 4, {}, None, 4)
    assert no_cost["16_cost_per_question_usd"] is None and no_cost["14_median_latency_s"] is None  # nothing measured is null, never zero


def test_the_sealed_holdout_is_run_only_with_final_and_only_for_a_holdout(tmp_path):
    items = [{"row": ROW, "evidence": ITEM["evidence"], "deterministic": {"outcome": "faq", "answer": "x", "sources": [], "conflicts": [], "candidates": []}}]
    with pytest.raises(SystemExit, match="--final"):
        B.run(args(evidence=str(frozen(tmp_path, items, "holdout")), deterministic=True))
    with pytest.raises(SystemExit, match="only for the holdout"):
        B.run(args(evidence=str(frozen(tmp_path, items, "fresh")), deterministic=True, final=True))
    with pytest.raises(SystemExit, match="sealed"):
        B.run(args(evidence=str(frozen(tmp_path, items, "locked")), deterministic=True))


def test_capturing_a_sealed_set_needs_an_explicit_flag():
    for name in ("locked", "verify", "holdout"):
        out = subprocess.run([sys.executable, str(ROOT / "scripts" / "llm_bench.py"), "capture", "--set", name, "--out", "/dev/null"], capture_output=True, text=True, timeout=60)
        assert out.returncode != 0 and "not captured for model comparison" in out.stderr + out.stdout


def test_the_final_holdout_is_unchanged_separate_from_every_other_set_and_covers_what_the_benchmark_measures():
    from collections import Counter
    for folder, prefix in (("unified", "ha"), ("reasoning", "hb")):
        base = ROOT / "examples" / folder
        raw = (base / "holdout.csv").read_bytes()
        assert hashlib.sha1(raw).hexdigest() == (base / "holdout.sha1").read_text().strip()  # edit a question or a label and this fails: the numbers would no longer be comparable
        rows = list(csv.DictReader((base / "holdout.csv").open(encoding="utf-8")))
        assert len(rows) >= 20 and all(r["id"].startswith(prefix) for r in rows) and len({r["id"] for r in rows}) == len(rows)
        assert {r["expected"] for r in rows} == {"answered", "partial", "conflict", "insufficient"}
        norm = lambda q: " ".join("".join(c for c in q.lower() if c.isalnum() or c == " ").split())  # noqa: E731
        mine = {norm(r["question"]) for r in rows}
        for other in ("dev", "regression", "fresh", "locked", "verify"):
            f = base / f"{other}.csv"
            if f.exists():
                theirs = {norm(r["question"]) for r in csv.DictReader(f.open(encoding="utf-8"))}
                theirs_ids = {r["id"] for r in csv.DictReader(f.open(encoding="utf-8"))}
                assert not (mine & theirs), f"{other} shares a question with the holdout"  # (compared by program: the sealed sets are never read question by question)
                assert not ({r["id"] for r in rows} & theirs_ids)
        kinds = Counter(r["category"] for r in rows)
        assert any(k in kinds for k in ("structured", "limit_check")) and "conflict" in kinds and "missing" in kinds and ("multi_source" in kinds or "multi_condition" in kinds)


def test_a_holdout_run_is_aggregate_only_and_recorded(tmp_path, monkeypatch):
    """Run the harness on a one-question holdout with the deterministic baseline: the report has no per-category breakdown and the ledger gains a line."""
    monkeypatch.setattr(B, "ROOT", tmp_path)
    items = [{"row": ROW, "evidence": ITEM["evidence"], "deterministic": {"outcome": "grounded", "answer": "The Growth plan costs 79 USD [1].", "sources": [{"source": "plans.csv", "location": "plans.csv › Growth"}],
                                                                       "conflicts": [], "candidates": [{"source": "plans.csv"}]}}]
    out = tmp_path / "out.json"
    B.run(args(evidence=str(frozen(tmp_path, items, "holdout")), deterministic=True, final=True, json=str(out)))
    report = json.loads(out.read_text())
    assert "by_category_correct" not in report and "by_source_type_correct" not in report and list(report["metrics"])[:2] == SEVENTEEN[:2]
    ledger = (tmp_path / "runs" / "llm" / "holdout.ledger.jsonl").read_text().splitlines()
    assert len(ledger) == 1 and json.loads(ledger[0])["label"] == "deterministic"


def test_extra_request_fields_reach_the_production_adapter(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "bench-key-0123456789")
    prov = B.make_provider(args(provider="openrouter", model="qwen/qwen3.5-9b", mode="schema", extra='{"reasoning": {"enabled": false}, "seed": 7}'))
    assert prov.adapter.extra_body["seed"] == 7 and prov.adapter.extra_body["reasoning"] == {"enabled": False}
