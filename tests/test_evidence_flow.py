"""Evidence-aware answers through the real website flow, with a real model, on the Pawfect pack (a dog groomer's FAQ and handbook).

Slow (trains a model), so it only runs with FAQENGINE_SLOW=1.
"""
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("FAQENGINE_SLOW"), reason="set FAQENGINE_SLOW=1 to run the model tests")

PAWFECT = Path(__file__).resolve().parents[1] / "examples" / "pawfect"


def test_faq_then_handbook_then_an_honest_no(site):
    pid = site.j("POST", "/api/projects", {"name": "pawfect", "files": [{"filename": p.name, "text": p.read_text()} for p in (PAWFECT / "pawfect_faq.csv", PAWFECT / "pawfect_handbook.md")]})["id"]
    site.j("POST", f"/api/projects/{pid}/train", {})
    assert site.wait(pid, "status", {"trained", "failed"}) == "trained", site.store.status(pid)

    def ask(q):
        return site.j("POST", f"/api/projects/{pid}/test", {"query": q})["response"]

    pets = ask("Which pets do you groom?")  # the FAQ answers it fully: stored answer, nothing else consulted
    assert pets["outcome"] == "faq" and "dogs only" in pets["answer"] and pets["used_llm"] is False and pets["sources"][0]["kind"] == "faq"
    hours = ask("What time does the studio close on Sunday?")  # only the handbook has it: grounded, with its section as the reference
    assert hours["outcome"] == "grounded" and "7:00 PM" in hours["answer"] and "Opening schedule" in hours["sources"][0]["location"] and hours["sources"][0]["source"] == "pawfect_handbook.md"
    late = ask("What fee applies if I collect my dog late?")
    assert late["outcome"] == "grounded" and "₹100" in late["answer"]
    for q in ("How much does a bath cost?", "Do you offer dog daycare?", "What is the capital of France?"):  # not in the FAQ or the handbook
        r = ask(q)
        assert r["outcome"] == "insufficient" and r["answer"] is None and r["escalate"] is True, (q, r["outcome"])
    for r in (pets, hours):
        assert {"retrieval_similarity", "answer_sufficiency", "evidence_support", "response_confidence"} <= set(r)
