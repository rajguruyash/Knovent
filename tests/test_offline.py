"""Local-only mode needs nothing from the outside: no Supabase, no language-model provider, no embedding service, no internet. Proven by forbidding every network connection and
running the whole answering workflow, with the real embedding model, on a real exported package."""
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "dist" / "bike-shop"
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(not (PACKAGE / "model").is_dir(), reason="needs the exported example package in dist/bike-shop")


@pytest.fixture()
def no_network(monkeypatch):
    attempts: list = []

    def refuse(*args, **kw):
        attempts.append(args[:2] if args else kw)
        raise OSError("network access is forbidden in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    return attempts


def test_the_complete_answering_workflow_runs_with_the_network_forbidden(no_network, tmp_path):
    from faqengine.runtime.engine import Engine

    engine = Engine(PACKAGE, state_dir=tmp_path)
    assert engine.respond("What are your opening hours?")["outcome"] == "faq"  # a stored answer
    r = engine.respond("How long does a repair take and how much does a basic service cost?")  # two parts, two entries
    assert r["outcome"] in ("faq", "grounded") and r["llm"]["enabled"] is False and r["used_llm"] is False
    declined = engine.respond("Do you sell second-hand car tyres?")  # not in the knowledge base: an honest no
    assert declined["outcome"] in ("insufficient", "clarification") and declined["answer"] is None
    assert engine.respond("what about that?")["outcome"] == "clarification"
    hit = engine.respond("How do I return an item?")
    cand = hit["retrieval"]["candidates"][0]
    assert cand["entry_id"] == "returns" and cand["retrieval_method"].startswith("hybrid") and {"similarity_score", "keyword_score", "combined_score"} <= set(cand)
    assert hit["retrieval"]["diagnostics"]["method"] == "hybrid" and hit["retrieval"]["diagnostics"]["embed_ms"] >= 0
    assert engine.learn("returns", "can I post it back")["added"] is True  # learning from feedback is local too
    assert engine.respond("can I post it back")["outcome"] == "faq"
    assert engine.answer("How do I return an item?")["decision"] in ("direct", "llm")  # the older decision interface
    assert no_network == [], f"something tried to use the network: {no_network}"


def test_nothing_in_local_mode_reads_a_language_model_setting_or_key(no_network, tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "FAQENGINE_LLM_API_KEY"):
        monkeypatch.setenv(name, "sk-should-never-be-used-123456")
    from faqengine.runtime.engine import Engine

    r = Engine(PACKAGE, state_dir=tmp_path).respond("What are your opening hours?")
    assert r["llm"]["enabled"] is False and r["llm"]["provider"] is None and no_network == []
