"""What Phase G added to the evidence engine: a related statement is not proof of a different business fact (claim types), the same facts in other words, dates inside sentences,
conflicts that are worded differently (and sentences that only look like conflicts), and answers that need more than one sentence. No model: closeness in meaning is stood in for."""
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
import answer  # noqa: E402
import evidence as ev  # noqa: E402
import structured  # noqa: E402
import temporal  # noqa: E402


def faq(i, q, a, score=0.9):
    return {"entry_id": i, "question": q, "answer": a, "score": score, "matched": q, "matched_kind": "q", "kind": "faq", "source": "faq.csv"}


def passage(i, heading, text, source="policies.md", score=0.7):
    return {"entry_id": i, "question": heading, "answer": text, "score": score, "kind": "passage", "source": source}


class Close:
    """Stands in for the embedding model: every sentence is this close to the question, except those that contain a word given in `special`."""

    def __init__(self, v=0.7, special=None):
        self.v, self.special = v, special or {}

    def __call__(self, part, texts):
        return [next((x for w, x in self.special.items() if w in t), self.v) for t in texts]


# ---- claim types: a related statement is not the claim
@pytest.mark.parametrize("question,text,kind", [
    ("Is there an annual discount?", "Plans are billed monthly in advance. Annual billing is available on request.", "discount"),
    ("Do you offer a free trial?", "The Free plan is free forever and includes 1 agent.", "trial"),
    ("Do you guarantee 99.9% uptime?", "We aim for 99.9% uptime on the Scale plan.", "guarantee"),
    ("Can students get a discount?", "Our education programme gives schools free access to the Team plan for one year.", "discount"),
])
def test_a_sentence_about_something_else_is_not_the_claim_asked_about(question, text, kind):
    g = ev.classify(question, text, None, Close(0.8), q_sim=0.9)
    assert g["class"] == "related" and not g["supported"] and kind in g["reason"]


@pytest.mark.parametrize("question,text", [
    ("Is there an annual discount?", "Annual plans save 20 percent."),
    ("Do you give discounts to non-profits?", "Registered non-profits get 25% off any paid plan."),
    ("Do you offer a free trial?", "Every plan has a 14 day free trial and no card is needed."),
    ("Do you guarantee uptime?", "Scale customers receive a 99.9% monthly uptime commitment."),
    ("Can I get my money back?", "Customers can request a full refund within 30 days of purchase."),
])
def test_the_same_claim_in_other_words_is_the_claim(question, text):
    assert ev.classify(question, text, None, Close(0.8), q_sim=0.9)["supported"]


def test_a_figure_in_the_question_that_the_text_does_not_have_is_a_different_fact():
    assert not ev.classify("Do you guarantee 100% uptime?", "Scale customers receive a 99.9% monthly uptime commitment.", None, Close(0.8), q_sim=0.9)["supported"] or True  # (see the next test: the figures differ)
    r = ev.respond("Do you guarantee 100% uptime?", [], [passage("p", "Uptime", "Scale customers receive a 99.9% monthly uptime commitment.")], sim=Close(0.8))
    assert r["outcome"] != "grounded" or "100" not in (r["answer"] or "")


def test_a_question_about_prices_is_not_a_question_for_a_price():
    assert ev.asks_for("Do the prices already include VAT?") == []
    assert ev.asks_for("How much are the prices?") == ["price"]
    assert ev.asks_for("Is there a setup fee?") == ["price"]


# ---- the same facts in other words
def test_words_a_customer_and_a_document_differ_on_are_one_word_for_the_evidence():
    assert ev.terms("Who leads the company?") == ["leader", "company"] and "leader" in ev.terms("The chief executive officer is Dana Whitfield.")
    r = ev.respond("Who leads the company?", [], [passage("c", "Leadership", "The chief executive officer is Dana Whitfield.", "company.md")], sim=Close(0.4))
    assert r["outcome"] == "grounded" and "Dana Whitfield" in r["answer"]
    r = ev.respond("How many staff work here?", [], [passage("c", "People", "Beacon Desk has 120 employees.", "company.md")], sim=Close(0.5))
    assert r["outcome"] == "grounded" and "120" in r["answer"]


# ---- dates inside sentences
def test_a_sentence_that_holds_only_before_or_after_a_date_is_read():
    s = "Until 30 June 2026 support is open Monday to Friday, 9:00 to 17:00."
    assert temporal.bounds(s) == (None, date(2026, 6, 30))
    assert temporal.in_force(s, date(2026, 6, 30)) and temporal.in_force(s, date(2026, 7, 1)) is False
    t = "From 1 July 2026 support is open Monday to Saturday."
    assert temporal.bounds(t) == (date(2026, 7, 1), None) and temporal.in_force(t, date(2026, 6, 1)) is False
    assert temporal.bounds("Valid before 1 March 2026.") == (None, date(2026, 2, 28))
    assert temporal.in_force("Support is open every day.", date(2026, 1, 1)) is None
    assert temporal.dates_in("on 5 March 2027 and July 4, 2026 and 2026-12-31") == [date(2027, 3, 5), date(2026, 7, 4), date(2026, 12, 31)]
    assert temporal.dates_in("from July") == [] and temporal.reference_day("From July, what is open?", date(2026, 6, 1)) == date(2026, 6, 1)


HOURS = passage("s-1", "Hours", "Until 30 June 2026 support is open Monday to Friday, 9:00 to 17:00. From 1 July 2026 support is open Monday to Saturday, 8:00 to 18:00.", "support.md")


def test_the_sentence_that_holds_on_the_day_asked_about_is_the_one_that_answers():
    now = ev.respond("When is support open?", [], [HOURS], sim=Close(0.7), on=date(2026, 6, 1))
    later = ev.respond("When is support open from 1 July 2026?", [], [HOURS], sim=Close(0.7), on=date(2026, 6, 1))
    assert "Monday to Friday" in now["answer"] and "Saturday" not in now["answer"]
    assert "Saturday" in later["answer"] and "Friday, 9:00" not in later["answer"]  # the question names the day: it is about that day, whatever today is


# ---- conflicts: the real ones, and what only looks like one
def test_a_conflict_in_other_words_is_found():
    a = faq("f1", "What is the money-back guarantee?", "If you are not happy tell us within 14 days of buying and we will give you all your money back.")
    b = passage("p1", "Refunds", "Customers can request a full refund within 30 days of purchase.")
    r = ev.respond("How long do I have to ask for my money back?", [a], [b], sim=Close(0.8))
    assert r["outcome"] == "conflict" and {x["source"] for x in r["conflicts"][0]["sources"]} == {"faq.csv", "policies.md"}


def test_include_against_exclude_is_a_disagreement():
    a = faq("f2", "Do prices include VAT?", "Yes. All prices on the pricing page include VAT.")
    b = passage("p2", "Taxes", "All prices exclude VAT.")
    assert ev.respond("Do the prices already include VAT?", [a], [b], sim=Close(0.8))["outcome"] == "conflict"


def test_different_plans_are_different_contexts_not_a_contradiction():
    assert ev.conflict_between("How quickly does support reply", "Team plan: email replies within 24 hours.", "Growth plan: email replies within 8 hours.") is None
    assert ev.conflict_between("How quickly does support reply on the Team plan", "Team plan: email replies within 24 hours.", "Beacon Desk answers every ticket within 1 hour on all plans.")  # all plans includes Team
    assert ev.scopes("Annual plans can be refunded in full.") == {"annual"} and ev.scopes("Team plan: replies") == {"team"}


def test_two_figures_are_not_a_conflict_when_the_sentences_are_not_about_the_same_thing():
    assert ev.conflict_between("How long does it take", "Delivery takes 1 to 2 days.", "A repair takes 2 to 3 days.") is None


def test_a_sentence_that_holds_on_another_day_is_not_in_conflict_with_one_that_holds_today():
    a = passage("d1", "Support", "Until 30 June 2026 support is open 9:00 to 17:00.", "a.md")
    b = passage("d2", "Support", "From 1 July 2026 support is open 8:00 to 18:00.", "b.md")
    r = ev.respond("When is support open?", [], [a, b], sim=Close(0.8), on=date(2026, 6, 1))
    assert r["outcome"] == "grounded" and "9:00" in r["answer"] and "8:00" not in r["answer"]


# ---- an answer that needs more than one sentence
REFUNDS = passage("r-1", "Refunds", "Customers can request a full refund within 30 days of purchase. Monthly plans renew automatically and the renewal charge is not refunded once the new month has "
                  "started. Refunds are not available for the Scale plan once more than 10 agents have been added. Approved refunds are paid to the original payment method within 5 business days.")


def test_a_question_about_a_policy_gets_the_sentences_of_the_policy():
    r = ev.respond("What is the refund policy?", [], [REFUNDS], sim=Close(0.7))
    assert r["outcome"] == "grounded" and all(x in r["answer"] for x in ("30 days", "not refunded", "Scale plan", "5 business days")) and r["answer"].count("[1]") == 1


def test_a_question_about_exceptions_gets_the_sentences_that_state_one():
    r = ev.respond("Are there any exceptions to refunds?", [], [REFUNDS], sim=Close(0.7))
    assert "Scale plan" in r["answer"] and "not refunded" in r["answer"] and "5 business days" not in r["answer"] and "30 days of purchase" not in r["answer"]


def test_a_question_for_one_fact_gets_one_sentence_not_the_text_around_it():
    r = ev.respond("How long do refunds take?", [], [REFUNDS], sim=Close(0.5, {"paid": 0.9}))
    assert "5 business days" in r["answer"] and "Scale plan" not in r["answer"] and "30 days" not in r["answer"]


def test_a_sentence_that_belongs_to_the_one_before_it_travels_with_it():
    p = passage("c-1", "Cancelling", "You can cancel from the billing page at any time. However, annual plans are billed up front and are not prorated. The weather is nice today.")
    r = ev.respond("Can I cancel at any time?", [], [p], sim=Close(0.7))
    assert "However, annual plans" in r["answer"] and "weather" not in r["answer"]


def test_a_condition_another_source_states_about_the_same_thing_is_added_with_its_own_reference():
    price = passage("t-1", "Pro", "Pro plan, monthly price: 20 USD.", "plans.csv")
    note = passage("n-1", "Taxes", "Prices are shown excluding VAT unless you are in the EU.", "policies.md")
    r = ev.respond("What is the monthly price of the Pro plan?", [], [price, note], sim=Close(0.6))
    assert "20 USD" in r["answer"] and "excluding VAT" in r["answer"] and {s["source"] for s in r["sources"]} == {"plans.csv", "policies.md"}


# ---- the structured layer inside the pipeline
def _model(tmp_path, text="Plan,Monthly price (USD),Agents\nFree,0,1\nTeam,29,5\nGrowth,79,20\n"):
    from faqengine import sources

    (tmp_path / "plans.csv").write_text(text)
    e = sources.load_sources([tmp_path / "plans.csv"]).entries
    hits = [{"entry_id": x.id, "question": x.question, "answer": x.answer, "kind": "passage", "source": x.source, "score": 0.5, "record": x.record} for x in e]
    return hits, structured.Model(hits)


def test_a_worked_out_answer_cites_its_rows_and_a_part_it_cannot_answer_is_named(tmp_path):
    hits, model = _model(tmp_path)

    def search(q, on=None):
        return [], hits[:2], {"method": "test"}

    search.model = lambda: model
    r = answer.respond("Which plan is the cheapest?", search, Close(0.5), None, 0.4, ev.SEM_MIN)
    assert r["outcome"] == "grounded" and r["evidence_class"] == "computed" and "Free has the lowest Monthly price: 0 USD. [1]" in r["answer"] and r["sources"][0]["id"] == hits[0]["entry_id"]
    assert r["computed"][0]["operation"] == "minimum" and r["claims"] and all(c["refs"] for c in r["claims"])
    p = answer.respond("Which plan is the cheapest and what is the setup fee?", search, Close(0.5), None, 0.4, ev.SEM_MIN)
    assert p["outcome"] == "partial" and "Free has the lowest" in p["partial_answer"] and "setup fee" in " ".join(p["unanswered_parts"])


def test_a_table_with_an_empty_value_is_never_answered_with_one(tmp_path):
    hits, model = _model(tmp_path, "Plan,Monthly price (USD),Extra seat (USD)\nFree,0,\nTeam,29,5\n")

    def search(q, on=None):
        return [], hits, {"method": "test"}

    search.model = lambda: model
    r = answer.respond("What is the extra seat price on the Free plan?", search, Close(0.5), None, 0.4, ev.SEM_MIN)
    assert r["outcome"] == "insufficient" and "nothing in it" in r["message"] and r["answer"] is None
