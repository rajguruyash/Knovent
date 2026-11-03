"""The real-device check (scripts/local_e2e_check.py) run against the stand-in runtime: this proves the CHECK works (it blocks cleanly without a runtime, a confirmation or room, pairs and refuses what it
should, downloads only the one model it was told to, drives the whole chain over the companion's real signed HTTP service, reports honestly and keeps to a local-only policy), not that any real model
does. The real run is made with a real Ollama (docs/local-first.md)."""
import csv
import json
import sys
from collections import namedtuple
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
pytest.importorskip("psycopg")
pytest.importorskip("cryptography")
from fake_runtimes import FakeRuntime  # noqa: E402

import local_e2e_check as check  # noqa: E402

pytestmark = pytest.mark.skipif(not (ROOT / "dist" / "bike-shop" / "model").is_dir(), reason="needs an exported embedding model (dist/bike-shop/model)")


@pytest.fixture(autouse=True)
def no_registry(monkeypatch):
    monkeypatch.setattr(check, "registry_download_bytes", lambda tag: 1_321_000_000)  # (the real check reads the size from Ollama's public manifest; a test touches no network)


def first_sentence():
    row = next(csv.DictReader((ROOT / "examples" / "bike-shop" / "faq.csv").open(encoding="utf-8")))
    return row["answer"].split(". ")[0].rstrip(".") + "."


def answer_json():
    return json.dumps({"verdict": "answered", "claims": [{"text": first_sentence(), "evidence": ["E1"]}], "missing": []})


def test_without_a_runtime_the_check_is_blocked_and_says_how_to_unblock(capsys):
    with pytest.raises(SystemExit) as e:
        check.main(["--ollama-base", "http://127.0.0.1:9"])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert "BLOCKED: Ollama is not running" in err and "https://ollama.com/download" in err


def test_without_the_model_installed_and_no_download_asked_for_the_check_is_blocked_and_never_downloads(capsys):
    rt = FakeRuntime("ollama", models=("something-else:1b",))
    try:
        with pytest.raises(SystemExit) as e:
            check.main(["--ollama-base", rt.base, "--model", "llama3.2:1b"])
        assert e.value.code == 2 and "BLOCKED: no suitable model is installed" in capsys.readouterr().err
        assert [r for r in rt.requests if r["path"] == "/api/pull"] == []
    finally:
        rt.close()


def test_a_download_needs_the_confirmation_and_a_model_from_the_list(capsys):
    rt = FakeRuntime("ollama", models=())
    try:
        with pytest.raises(SystemExit) as e:
            check.main(["--ollama-base", rt.base, "--provision", "llama3.2-1b"])
        assert e.value.code == 2 and "needs your confirmation" in capsys.readouterr().err
        with pytest.raises(SystemExit) as e:
            check.main(["--ollama-base", rt.base, "--provision", "not-a-model", "--yes"])
        assert e.value.code == 2 and "not a model in the companion's list" in capsys.readouterr().err
        assert [r for r in rt.requests if r["path"] == "/api/pull"] == []
    finally:
        rt.close()


def test_a_download_is_refused_before_it_starts_when_the_disk_would_not_hold_the_model_and_nothing_is_removed(monkeypatch, capsys):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(check.shutil, "disk_usage", lambda p: usage(10**12, 10**12 - 10**9, 10**9))  # 1 GB free for a 1.3 GB model
    rt = FakeRuntime("ollama", models=())
    try:
        with pytest.raises(SystemExit) as e:
            check.main(["--ollama-base", rt.base, "--provision", "llama3.2-1b", "--yes"])
        err = capsys.readouterr().err
        assert e.value.code == 2 and "would not hold" in err and "never removes anything" in err
        assert [r for r in rt.requests if r["path"] in ("/api/pull", "/api/delete")] == []
    finally:
        rt.close()


def test_the_check_pairs_downloads_only_the_named_model_and_drives_the_whole_chain_on_a_local_only_policy(tmp_path, capsys):
    rt = FakeRuntime("ollama", answer=answer_json(), models=(), chunk_delay=0.02)
    out = tmp_path / "report.json"
    try:
        assert check.main(["--ollama-base", rt.base, "--provision", "llama3.2-1b", "--yes", "--questions", "2", "--out", str(out), "--timeout", "30", "--cancel-after", "0.1"]) == 0
    finally:
        rt.close()
    rep = json.loads(out.read_text())
    assert rep["model"] == "llama3.2:1b" and rep["policy"] == "local_only" and rep["steps"] == ["local"] and rep["device"] == "auto"
    assert rep["preflight"]["download_gb"] == pytest.approx(1.23, abs=0.01) and rep["preflight"]["free_disk_gb"] > 0 and rep["preflight"]["memory_before"] is not None  # what was checked before anything was downloaded
    pulls = [r["body"]["model"] for r in rt.requests if r["path"] == "/api/pull"]
    assert pulls == ["llama3.2:1b"]  # one model, the one named
    assert rep["download"]["state"] == "done" and rep["download"]["selected_by_companion"] is True
    assert rep["pairing"] == {"wrong_code_refused": True, "unsigned_request_refused": True, "paired": True, "second_use_refused": True, "status_codes": rep["pairing"]["status_codes"]}
    assert rep["health_before_first_answer"]["ok"] is True and rep["registered"] is True and rep["selected"]["model"] == "llama3.2:1b"
    q = rep["questions"]
    assert len(q) == 2 and q[-1]["question"].startswith("Do you rent submarines")
    assert q[0]["model_asked"] is True and q[0]["generated_by"] == "model" and q[0]["verified"] is True and q[0]["tier"] == "local" and q[0]["attempts"] == ["local"] and q[0]["model_seconds"] >= 0
    assert rep["external_inference_possible"]["operator_modes"] == ["local"] and all(v == "MISSING" for v in rep["external_inference_possible"]["platform_credentials"].values())
    assert rep["nothing_external_was_called"] is True and all(m["tier"] == "local" and m["mode"] == "local" for m in rep["metering"]) and rep["network"]["external_peers_outside_the_download"] == []
    c = rep["cancellation"]
    assert c["stopped"] is True and c["was_still_answering"] is True and c["seconds_to_stop"] < 5
    assert c["service_failure_kind"] == "provider:cancelled" and c["service_generated_by"] == "deterministic_fallback" and c["steps_tried"] == [["local", "cancelled"]] and c["nothing_else_tried"] is True  # a cancel tried nothing else
    assert "peak_ollama_mb" in rep["memory"] and rep["memory"]["after"] is not None and rep["summary"]["asked"] == 2 and any("do not measure how good the model is" in n for n in rep["notes"])
    assert "SOMETHING EXTERNAL" not in capsys.readouterr().err


def test_a_model_already_installed_is_not_downloaded_again(tmp_path):
    rt = FakeRuntime("ollama", answer=answer_json(), models=("llama3.2:1b",), chunk_delay=0.02)
    try:
        assert check.main(["--ollama-base", rt.base, "--provision", "llama3.2-1b", "--yes", "--questions", "1", "--out", str(tmp_path / "r.json"), "--timeout", "30", "--cancel-after", "0.1"]) == 0
        assert [r for r in rt.requests if r["path"] == "/api/pull"] == []
    finally:
        rt.close()
