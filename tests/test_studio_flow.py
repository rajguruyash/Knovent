"""The whole website flow through the real server and real worker processes: upload, train, ask, judge, export, download, retrain.

Slow (trains a model and exports ONNX), so it only runs with FAQENGINE_SLOW=1.
"""
import io
import json
import os
import subprocess
import sys
import urllib.error
import zipfile

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")


def test_upload_train_ask_judge_export_download_and_retrain(site, tmp_path):
    pid = site.j("POST", "/api/projects/example", {})["id"]

    # train: a separate process, with live progress
    site.j("POST", f"/api/projects/{pid}/train", {})
    with pytest.raises(urllib.error.HTTPError):  # one job at a time
        site.j("POST", f"/api/projects/{pid}/train", {})
    assert site.wait(pid, "status", {"trained", "failed"}) == "trained", site.store.status(pid)
    p = site.j("GET", f"/api/projects/{pid}")
    r = p["report"]
    assert r["faq"]["entries"] == 10 and r["model"]["vectors"] == 21 and r["selfcheck"]["systems"]["tuned"]["hit@1"] > 0.5 and r["map"]["points"]
    assert p["thresholds"]["direct"] is None and p["thresholds"]["floor"] > 0  # nothing proven yet: no direct answers
    assert p["progress"] is None and p["status"]["state"] == "trained"

    # ask
    t = site.j("POST", f"/api/projects/{pid}/test", {"query": "can i try a bike before buying"})
    assert t["hits"][0]["id"] == "test-ride" and t["band"] in ("llm", "direct") and t["point"] and t["latency_ms"] < 5000
    off = site.j("POST", f"/api/projects/{pid}/test", {"query": "who won the football last night"})
    assert off["band"] == "none"

    # judge
    fb = site.j("POST", f"/api/projects/{pid}/feedback", {"test_id": t["test_id"], "verdict": "approve", "comment": "right"})
    assert fb["feedback"]["tally"]["approved"] == 1
    fb = site.j("POST", f"/api/projects/{pid}/feedback", {"test_id": off["test_id"], "verdict": "approve", "comment": "correct refusal"})
    assert fb["feedback"]["tally"]["tested"] == 2

    # export, download, and run what was downloaded
    site.j("POST", f"/api/projects/{pid}/export", {})
    assert site.wait(pid, "export", {"done", "failed"}) == "done", site.store.export_status(pid)
    blob = site.call("GET", f"/api/projects/{pid}/package.zip")
    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
    for needed in ("model/tokenizer.json", "kb/embeddings.npy", "runtime/engine.py", "runtime/serve.py", "thresholds.json", "eval/report.md", "README.md"):
        assert any(n.endswith(needed) for n in names), needed
    zipfile.ZipFile(io.BytesIO(blob)).extractall(tmp_path)
    root = next(tmp_path.iterdir())
    th = json.loads((root / "thresholds.json").read_text())
    assert th["direct"] is None and abs(th["floor"] - site.j("GET", f"/api/projects/{pid}")["thresholds"]["floor"]) < 1e-9
    out = subprocess.run([sys.executable, str(root / "runtime" / "engine.py"), str(root), "can i try a bike before buying"], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    assert json.loads(out.stdout)["hits"][0]["entry_id"] == "test-ride"

    # retrain using what was judged: a new model generation, and the old judgements no longer count for it
    site.j("POST", f"/api/projects/{pid}/train", {"use_feedback": True})
    assert site.wait(pid, "status", {"trained", "failed"}) == "trained", site.store.status(pid)
    p = site.j("GET", f"/api/projects/{pid}")
    assert p["gen"] == 2 and p["feedback"]["tally"]["tested"] == 0 and p["feedback"]["earlier"] == 2 and p["export"]["state"] == "idle"
    kb = json.loads((site.store.run_dir(pid) / "kb.json").read_text())
    assert any("try a bike before buying" in " ".join([e["question"], *e.get("alt_questions", [])]).lower() for e in kb if e["id"] == "test-ride")  # the approved question was kept as wording
