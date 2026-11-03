"""Paraphrase generation: parsing, tidying, caching, the HTTP client against a local fake server, and the benchmark hook."""
import json
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pytest

from faqengine import bench
from faqengine import paraphrase as pp
from faqengine.kb import Entry, load_kb, write_kb


def test_parse_accepts_arrays_fences_and_plain_lists():
    assert pp.parse('["a b", "c d"]') == ["a b", "c d"]
    assert pp.parse('Here you go:\n```json\n["one", "two"]\n```') == ["one", "two"]
    assert pp.parse("1. first way\n2) second way\n- third way\n```") == ["first way", "second way", "third way"]
    assert pp.parse('["ok", {"x": 1}, 3]') == ["ok", "3"]  # only scalars survive


def test_tidy_drops_the_original_duplicates_and_junk():
    out = pp.tidy("How do I reset my password?", ["how do i reset my password?", "forgot password", "Forgot Password", "", " 'reset pw' ", "x" * 300, "new login needed"], 3)
    assert out == ["forgot password", "reset pw", "new login needed"]


def test_prompt_mentions_the_question_and_only_includes_an_answer_when_there_is_one():
    assert "FAQ answer: We open at 9." in pp.make_prompt("When do you open?", "We open at 9.", 5)
    assert "FAQ answer" not in pp.make_prompt("When do you open?", "", 5)
    assert "JSON array of 5 strings" in pp.make_prompt("q", "", 5)


def test_generate_caches_resumes_and_reports_short_answers(tmp_path):
    entries = [Entry("a", ["Q one?"], "A1"), Entry("b", ["Q two?"], "A2"), Entry("c", ["Q three?"], "A3")]
    calls = []

    def ask(prompt):
        calls.append(prompt)
        if "Q three?" in prompt:
            return '["only one"]'
        return json.dumps([f"variant {len(calls)}a", f"variant {len(calls)}b", f"variant {len(calls)}c"])

    cache = pp.Cache(tmp_path / "c.json", "m", 3)
    stats = pp.generate(entries, 3, ask, cache, progress=lambda m: None)
    assert stats == {"entries": 3, "asked": 3, "cached": 0, "short": 1}
    again = pp.generate(entries, 3, ask, pp.Cache(tmp_path / "c.json", "m", 3), progress=lambda m: None)
    assert again["asked"] == 0 and again["cached"] == 3 and len(calls) == 3  # nothing repeated
    expanded = pp.expand(entries, pp.Cache(tmp_path / "c.json", "m", 3))
    assert expanded[0].questions[0] == "Q one?" and len(expanded[0].questions) == 4 and len(expanded[2].questions) == 2
    with pytest.raises(pp.LLMError, match="made with model"):
        pp.Cache(tmp_path / "c.json", "other-model", 3)


def test_a_crash_midway_keeps_what_was_done(tmp_path):
    entries = [Entry("a", ["Q one?"], ""), Entry("b", ["Q two?"], "")]

    def ask(prompt):
        if "Q two?" in prompt:
            raise pp.LLMError("HTTP 429")
        return '["x y", "z w"]'

    with pytest.raises(pp.LLMError):
        pp.generate(entries, 2, ask, pp.Cache(tmp_path / "c.json", "m", 2), progress=lambda m: None)
    assert pp.Cache(tmp_path / "c.json", "m", 2).get("Q one?", "") == ["x y", "z w"]


def test_expanded_faq_round_trips_through_csv_and_json(tmp_path):
    entries = [Entry("a", ["Q one?", "alt | pipe"], "A1", "cat"), Entry("b", ["Q two?"], "A2")]
    for name in ("x.csv", "x.json"):
        write_kb(entries, tmp_path / name)
        kb = load_kb(tmp_path / name)
        assert [e.id for e in kb.entries] == ["a", "b"]
        assert kb.entries[0].questions[0] == "Q one?" and len(kb.entries[0].questions) == 2 and kb.entries[0].category == "cat"


class _Fake(BaseHTTPRequestHandler):
    seen: list = []
    script: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Fake.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        status, payload = _Fake.script.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", "0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


@pytest.fixture()
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _Fake.seen, _Fake.script = [], []
    yield f"http://127.0.0.1:{httpd.server_port}/v1"
    httpd.shutdown()


def _ok(text):
    return 200, {"choices": [{"message": {"content": text}}]}


def test_client_speaks_the_openai_chat_format_and_sends_the_key(server):
    _Fake.script = [_ok('["a b"]')]
    out = pp.ChatClient(server, "some-model", "sekret").complete("hello")
    assert out == '["a b"]'
    sent = _Fake.seen[0]
    assert sent["path"] == "/v1/chat/completions" and sent["auth"] == "Bearer sekret"
    assert sent["body"]["model"] == "some-model" and sent["body"]["messages"] == [{"role": "user", "content": "hello"}]


def test_client_retries_rate_limits_then_succeeds_and_fails_clearly_otherwise(server):
    _Fake.script = [(429, {"error": "slow down"}), (503, {"error": "busy"}), _ok("fine")]
    assert pp.ChatClient(server, "m", "k").complete("x") == "fine" and len(_Fake.seen) == 3
    _Fake.script = [(401, {"error": {"message": "API key not valid"}})]
    with pytest.raises(pp.LLMError, match="HTTP 401.*API key not valid"):
        pp.ChatClient(server, "m", "bad").complete("x")
    assert len(_Fake.seen) == 4  # a 401 is not retried
    _Fake.script = [(200, {"unexpected": True})]
    with pytest.raises(pp.LLMError, match="unexpected reply"):
        pp.ChatClient(server, "m", "k").complete("x")


def test_key_must_come_from_the_environment_except_for_a_local_server(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    with pytest.raises(pp.LLMError, match="NOPE_KEY"):
        pp.client_from_env("https://example.com/v1", "m", "NOPE_KEY", 0)
    assert pp.client_from_env("http://localhost:11434/v1", "llama", "NOPE_KEY", 0).api_key is None  # Ollama needs none
    monkeypatch.setenv("NOPE_KEY", "abc")
    assert pp.client_from_env("https://example.com/v1", "m", "NOPE_KEY", 0).api_key == "abc"


def _fake_data():
    rng = random.Random(0)
    return {f"intent_{i}": {"train": [f"intent {i} train question number {j} {rng.random()}" for j in range(14)],
                            "test": [f"intent {i} test question {j}" for j in range(4)]} for i in range(6)}


def test_benchmark_llm_scenario_keeps_everything_but_the_extra_phrasings(tmp_path):
    data = _fake_data()
    plain = bench.build_scenario(data, seed=1, n_intents=4, k=1)
    cache = pp.Cache(tmp_path / "p.json", "m", 3)
    for e in plain.kb.entries:
        cache.put(e.question, e.answer, [f"{e.id} paraphrase {i}" for i in range(3)])
    with_llm = bench.build_scenario(data, seed=1, n_intents=4, k=1, paraphrases=cache, use=2)
    for a, b in zip(plain.kb.entries, with_llm.kb.entries):
        assert b.questions[0] == a.questions[0] and len(b.questions) == 3 and b.answer == a.answer
    assert plain.dev.texts == with_llm.dev.texts and plain.test.texts == with_llm.test.texts
    assert np.array_equal(plain.test.gold, with_llm.test.gold)
    with pytest.raises(ValueError, match="k=1"):
        bench.build_scenario(data, seed=1, n_intents=4, k=2, paraphrases=cache)
    with pytest.raises(ValueError, match="bench-paraphrases"):
        bench.build_scenario(data, seed=2, n_intents=4, k=1, paraphrases=cache)  # a different seed has other entries: nothing cached
