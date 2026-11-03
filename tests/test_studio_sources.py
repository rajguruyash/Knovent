"""Several files as one knowledge base, documents on their own, and a typed held-out set, through the real server and worker processes.

Slow (each trains or loads the model), so it only runs with FAQENGINE_SLOW=1.
"""
import csv
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")

EXAMPLE = Path(__file__).resolve().parents[1] / "src" / "faqengine" / "studio" / "static" / "example-bike-shop.csv"
HANDBOOK = """# Storage

We keep a repaired bike for thirty days after we call you. After that a storage fee of two pounds a day applies.

# Opening on holidays

On public holidays the workshop is closed, but the shop opens from ten until two for sales and collections.
"""
STORAGE = "is there a storage fee if I leave my bike at the shop"

HELDOUT = Path(__file__).resolve().parents[1] / "examples" / "bike-shop" / "heldout.csv"  # typed questions written in my own words, not the FAQ's phrasings


def heldout_split_counts() -> dict:
    rows = list(csv.DictReader(HELDOUT.open(newline="")))
    return {"total": len(rows), "dev": sum(r["split"] == "dev" for r in rows), "test": sum(r["split"] == "test" for r in rows)}


def project(site, name, *files):
    return site.j("POST", "/api/projects", {"name": name, "files": [{"filename": n, "text": t} for n, t in files]})["id"]


def train(site, pid):
    site.j("POST", f"/api/projects/{pid}/train", {})
    assert site.wait(pid, "status", {"trained", "failed"}) == "trained", site.store.status(pid)
    return site.j("GET", f"/api/projects/{pid}")


def test_a_faq_and_a_document_are_one_knowledge_base(site, tmp_path):
    pid = project(site, "Bike shop and handbook", ("faq.csv", EXAMPLE.read_text()), ("handbook.md", HANDBOOK))
    p = site.j("GET", f"/api/projects/{pid}")
    assert p["faq"]["entries"] == 10 and p["faq"]["passages"] == 2
    assert [(s["name"], s["kind"]) for s in p["faq"]["sources"]] == [("faq.csv", "faq"), ("handbook.md", "document")]

    r = train(site, pid)["report"]
    assert r.get("mode", "selfcheck") == "selfcheck" and r["faq"]["entries"] == 10 and r["faq"]["passages"] == 2 and r["model"]["vectors"] > 12
    assert len(r["faq"]["sources"]) == 2

    # the document answers what the FAQ cannot, and is labelled as a document
    t = site.j("POST", f"/api/projects/{pid}/test", {"query": STORAGE})
    assert t["hits"][0]["kind"] == "passage" and t["hits"][0]["source"] == "handbook.md" and t["band"] == "llm"

    # with the "answer directly" line at zero a FAQ question is answered directly; a passage never is
    site.j("POST", f"/api/projects/{pid}/settings", {"direct": 0.0})
    assert site.j("POST", f"/api/projects/{pid}/test", {"query": STORAGE})["band"] == "llm"
    assert site.j("POST", f"/api/projects/{pid}/test", {"query": "can i try a bike before buying"})["band"] == "direct"

    # the downloaded package behaves the same way
    site.j("POST", f"/api/projects/{pid}/export", {})
    assert site.wait(pid, "export", {"done", "failed"}) == "done", site.store.export_status(pid)
    zipfile.ZipFile(io.BytesIO(site.call("GET", f"/api/projects/{pid}/package.zip"))).extractall(tmp_path)
    root = next(tmp_path.iterdir())
    assert json.loads((root / "thresholds.json").read_text())["direct"] == 0.0
    entries = json.loads((root / "kb" / "entries.json").read_text())["entries"]
    assert {e["kind"] for e in entries} == {"faq", "passage"}
    out = subprocess.run([sys.executable, str(root / "runtime" / "engine.py"), str(root), STORAGE], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    answer = json.loads(out.stdout)
    assert answer["hits"][0]["kind"] == "passage" and answer["decision"] == "llm" and answer["answer"] is None


def test_documents_alone_are_searched_but_nothing_is_trained(site):
    pid = project(site, "Handbook only", ("handbook.md", HANDBOOK))
    p = site.j("GET", f"/api/projects/{pid}")
    assert p["faq"]["entries"] == 0 and p["faq"]["passages"] == 2
    r = train(site, pid)["report"]
    assert r["mode"] == "documents" and r["training"]["skipped"]
    t = site.j("POST", f"/api/projects/{pid}/test", {"query": STORAGE})
    assert t["hits"][0]["id"].startswith("handbook-") and t["band"] != "direct"


def test_typed_heldout_questions_are_reported_separately_and_the_lines_can_be_set(site):
    pid = site.j("POST", "/api/projects/example", {})["id"]
    summary = site.j("POST", f"/api/projects/{pid}/heldout", {"text": HELDOUT.read_text()})["heldout"]
    counts = heldout_split_counts()
    assert summary["total"] == counts["total"] and summary["by_split"] == {"dev": 0, "test": counts["total"]}  # every row is a test row, whatever its split column says

    p = train(site, pid)
    r, h = p["report"], p["report"]["heldout"]
    assert r["mode"] == "heldout" and h["counts"]["by_split"]["dev"] == 0 and h["counts"]["total"] <= summary["total"]  # a few more may go: copies of the validation questions are dropped too
    assert h["decision"]["ship"] in ("base", "tuned") and {"paraphrase", "typo"} <= set(h["test"]["by_type"])
    assert h["test"]["overall"]["base"]["hit@1"] > 0.5  # a sanity floor for the untouched base model on these easy questions
    assert {"base", "tuned"} <= set(h["refusal"]) and h["refusal"]["base"]["sweep"]
    assert not p["stale"]

    th = site.j("POST", f"/api/projects/{pid}/settings", {"floor": 0.5, "direct": 0.9})["thresholds"]
    assert th["floor"] == 0.5 and th["direct"] == 0.9
    assert site.j("GET", f"/api/projects/{pid}")["settings"]["manual"] == {"floor": 0.5, "direct": 0.9}
    th = site.j("POST", f"/api/projects/{pid}/settings", {"floor": None, "direct": None})["thresholds"]
    assert th["floor"] != 0.5 and th["direct"] is None  # back to what was calculated

    site.j("POST", f"/api/projects/{pid}/heldout/clear", {})
    assert site.j("GET", f"/api/projects/{pid}")["stale"]  # the model was trained with that held-out set


def test_a_corrected_question_is_searched_at_once_and_is_not_counted_as_accuracy(site):
    from faqengine.studio import server

    pid = site.j("POST", "/api/projects/example", {})["id"]
    train(site, pid)
    q = "zork blimp wumble"  # nothing in the FAQ resembles it
    first = site.j("POST", f"/api/projects/{pid}/test", {"query": q})
    assert first["hits"][0]["id"] != "delivery" and not first["hits"][0]["learned"]

    fb = site.j("POST", f"/api/projects/{pid}/feedback", {"test_id": first["test_id"], "verdict": "reject", "comment": "", "correct_entry": "delivery"})
    assert fb["learned"] == 1  # joined what is searched, with no retraining
    again = site.j("POST", f"/api/projects/{pid}/test", {"query": q})
    assert again["hits"][0]["id"] == "delivery" and again["hits"][0]["learned"] and again["hits"][0]["score"] > 0.99
    tally = site.j("POST", f"/api/projects/{pid}/feedback", {"test_id": again["test_id"], "verdict": "approve"})["feedback"]["tally"]
    assert tally["from_learned"] == 1 and tally["approved"] == 0 and tally["rejected"] == 1  # a memorised match is tested, not scored

    server.Handler.app.engines.pop(pid, None)  # the page's engine is rebuilt (a restart, say): what was approved is searched again from the history
    assert site.j("POST", f"/api/projects/{pid}/test", {"query": q})["hits"][0]["id"] == "delivery"


def test_changed_files_are_searched_without_training_and_every_reading_is_a_version(site):
    pid = project(site, "Lifecycle", ("handbook.md", HANDBOOK), ("hours.md", "# Reception\n\nThe reception desk takes bookings from nine until five.\n"))
    train(site, pid)
    sources = {s["name"]: s for s in site.j("GET", f"/api/projects/{pid}")["faq"]["sources"]}
    assert sources["hours.md"]["version"] == 1 and sources["hours.md"]["changed"] is None
    # the same name again with new content: a replacement (never a duplicate), a new version, and a note of what changed
    site.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "hours.md", "text": "# Reception\n\nThe reception desk takes bookings from ten until six.\n"}]})
    p = site.j("GET", f"/api/projects/{pid}")
    assert p["stale"] is True and p["faq"]["passages"] == 3
    hours = next(s for s in p["faq"]["sources"] if s["name"] == "hours.md")
    assert hours["version"] == 2 and hours["changed"] == {"added": 1, "removed": 1, "unchanged": 0}
    untouched = next(s for s in p["faq"]["sources"] if s["name"] == "handbook.md")
    assert untouched["version"] == 1 and untouched["changed"] is None  # a file this upload did not name keeps its version
    assert "nine" in site.j("POST", f"/api/projects/{pid}/test", {"query": "when does the reception desk take bookings"})["hits"][0]["answer"]  # the old index until it is updated
    done = site.j("POST", f"/api/projects/{pid}/reindex", {})
    assert done["ok"] and 0 < done["embedded"] <= 3 and done["kept"] >= 2 and done["dropped"] >= 1  # only the new text was embedded; the handbook's vectors were kept
    p = site.j("GET", f"/api/projects/{pid}")
    assert p["stale"] is False and p["reindexed"] is True and p["status"]["state"] == "trained" and p["gen"] == 1  # no training happened
    hit = site.j("POST", f"/api/projects/{pid}/test", {"query": "when does the reception desk take bookings"})["hits"][0]
    assert "ten" in hit["answer"] and "nine" not in hit["answer"]
    kb = json.loads((site.store.run_dir(pid) / "kb.json").read_text())
    assert not any("nine" in e["answer"] for e in kb) and sum("hours" in e["source"] for e in kb) == 1  # on disk too, and once
    # the same bytes again are not a new version
    site.j("POST", f"/api/projects/{pid}/sources", {"files": [{"filename": "hours.md", "text": "# Reception\n\nThe reception desk takes bookings from ten until six.\n"}]})
    assert next(s for s in site.j("GET", f"/api/projects/{pid}")["faq"]["sources"] if s["name"] == "hours.md")["version"] == 2
    # a source that is removed stops being searched after the index is updated
    site.j("POST", f"/api/projects/{pid}/sources/remove", {"name": "hours.md"})
    site.j("POST", f"/api/projects/{pid}/reindex", {})
    kb = json.loads((site.store.run_dir(pid) / "kb.json").read_text())
    assert not any("hours" in e["source"] for e in kb)
    assert "ten until six" not in " ".join(h["answer"] for h in site.j("POST", f"/api/projects/{pid}/test", {"query": "when does the reception desk take bookings"})["hits"])
    # training afterwards learns everything: a new model generation, and the note that only the index was updated is gone
    p = train(site, pid)
    assert p["gen"] == 2 and p["reindexed"] is False and p["stale"] is False
