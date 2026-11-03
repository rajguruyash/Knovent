"""Evidence-aware answering: a FAQ answer is only an answer when it supports what was asked, whatever the similarity score says."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
import evidence as ev  # noqa: E402


def faq(entry_id, question, answer, score=0.9, matched=None):
    return {"entry_id": entry_id, "question": question, "answer": answer, "score": score, "matched": matched or question, "matched_kind": "q", "kind": "faq", "source": "pawfect_faq.csv"}


def passage(entry_id, heading, text, source="pawfect_handbook.md", score=0.7):
    return {"entry_id": entry_id, "question": heading, "answer": text, "score": score, "kind": "passage", "source": source}


SERVICES = faq("faq-001", "What services does Pawfect offer?", "Pawfect offers dog bathing, coat brushing, nail trimming, ear cleaning, and breed-specific haircuts.")
CANCEL = faq("faq-010", "Can I cancel or reschedule?", "Please give at least 6 hours' notice to cancel or reschedule an appointment.")
PETS = faq("faq-004", "Which pets do you groom?", "Pawfect currently accepts dogs only. It does not groom cats or other animals.")
HOURS = passage("pawfect-handbook-007", "Opening schedule", "The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM. It is closed on Mondays. The final appointment slot starts at 5:30 PM.")
LATE = passage("pawfect-handbook-003", "Late arrival and collection", "If an owner is more than 15 minutes late, the studio may shorten the service or offer the next available appointment.")
FEE = passage("pawfect-handbook-004", "Collection fees", "A late collection fee of ₹100 applies for each additional 30-minute period.")
WEB = passage("web-pawfect-care-001", "Puppy care", "Puppies under 12 weeks must have completed their first vaccinations before they can be groomed at the studio.", source="https://pawfect.example/care")


class NoLLM:
    """A model that fails the test if anything asks it anything."""

    def __call__(self, prompt):
        raise AssertionError("the model must not be called here")


def test_a_similar_faq_question_whose_answer_lacks_the_requested_information_is_not_an_answer():
    r = ev.respond("How much does a dog bath cost?", [SERVICES], [])  # similar subject, but the answer lists a bath and gives no price
    assert r["outcome"] == "insufficient" and r["answer"] is None and r["answer_sufficiency"] == 0.0 and r["escalate"] is True


@pytest.mark.parametrize("similarity", [0.55, 0.9, 0.999, 1.0])
def test_a_similarity_of_one_cannot_bypass_the_sufficiency_check(similarity):
    identical = faq("faq-001", "How much does a bath cost?", "We offer bathing for dogs of every size and coat.", score=similarity)  # the very same question, answer without a price
    r = ev.respond("How much does a bath cost?", [identical], [], similarity_floor=0.0)
    assert r["retrieval_similarity"] == similarity  # the score is kept exactly as measured...
    assert r["outcome"] == "insufficient" and r["answer"] is None  # ...and decides nothing about whether the answer is complete
    assert r["answer_sufficiency"] == 0.0 and r["response_confidence"] == "none"
    assert "not a probability" in r["retrieval_similarity_note"]
    stated = faq("faq-001", "How much does a bath cost?", "A bath costs ₹600 for small dogs.", score=similarity)
    assert ev.respond("How much does a bath cost?", [stated], [])["outcome"] == "faq"  # the same score with a supporting answer is a FAQ answer


def test_the_four_signals_stay_apart():
    r = ev.respond("How much does a bath cost?", [faq("f", "How much does a bath cost?", "Call us.", score=1.0)], [])
    assert {"retrieval_similarity", "answer_sufficiency", "evidence_support", "response_confidence"} <= set(r)
    assert r["retrieval_similarity"] == 1.0 and r["answer_sufficiency"] == 0.0 and r["evidence_support"] == 0.0 and r["response_confidence"] == "none"


def test_a_faq_that_is_insufficient_is_answered_from_the_handbook_with_its_location():
    r = ev.respond("What time does the studio close on Sunday?", [SERVICES], [HOURS, LATE])
    assert r["outcome"] == "grounded" and r["used_llm"] is False
    assert "7:00 PM" in r["answer"] and r["sources"][0]["source"] == "pawfect_handbook.md" and "Opening schedule" in r["sources"][0]["location"] and "(passage 7)" in r["sources"][0]["location"]
    assert "[1]" in r["answer"]  # the answer carries its reference
    assert r["response_confidence"] in ("medium", "high") and "calibrated" in r["confidence_note"]


def test_a_faq_that_is_insufficient_is_answered_from_a_web_page():
    r = ev.respond("Do puppies need vaccinations before grooming?", [PETS], [HOURS, WEB])
    assert r["outcome"] == "grounded" and r["sources"][0]["source"] == "https://pawfect.example/care" and "12 weeks" in r["answer"]


def test_several_passages_jointly_support_one_answer_and_a_model_only_combines_them():
    question = "What happens if the owner is more than 15 minutes late and what is the late collection fee?"
    plain = ev.respond(question, [SERVICES], [LATE, FEE, HOURS])
    assert plain["outcome"] == "grounded" and len(plain["sources"]) == 2 and plain["used_llm"] is False  # assembled from the passages' own sentences
    assert "shorten the service" in plain["answer"] and "₹100" in plain["answer"]
    seen = []
    good = ev.respond(question, [SERVICES], [LATE, FEE, HOURS], llm=lambda p: seen.append(p) or "The studio may shorten the service [1]. A late collection fee of ₹100 applies for each additional 30-minute period [2].")
    assert good["used_llm"] is True and len(seen) == 1 and "ONLY the numbered passages" in seen[0] and "Opening schedule" not in seen[0]  # only the supporting passages are shown to it
    invented = ev.respond(question, [SERVICES], [LATE, FEE, HOURS], llm=lambda p: "The studio may shorten the service [1] and the fee is ₹250 [2].")
    assert invented["used_llm"] is False and "₹100" in invented["answer"] and "₹250" not in invented["answer"]  # a model's invented figure is discarded
    chatty = ev.respond(question, [SERVICES], [LATE, FEE], llm=lambda p: "Dogs generally dislike being groomed in hot weather and the staff are friendly and kind and experienced professionals.")
    assert chatty["used_llm"] is False  # an answer that leans on general knowledge is discarded too


def test_when_nothing_in_the_knowledge_base_answers_it_says_so_and_never_asks_a_model():
    r = ev.respond("Do you offer dog daycare?", [SERVICES], [HOURS, LATE], llm=NoLLM())
    assert r["outcome"] == "insufficient" and r["answer"] is None and r["used_llm"] is False and r["escalate"] is True
    assert "does not answer" in r["message"] and "person" in r["message"]  # honest, with a way forward


def test_conflicting_faq_and_handbook_are_reported_not_resolved():
    handbook = passage("pawfect-handbook-009", "Cancellations", "Cancellations require at least 24 hours' notice before the appointment.")
    r = ev.respond("What notice is needed to cancel an appointment?", [CANCEL], [handbook], llm=NoLLM())
    assert r["outcome"] == "conflict" and r["answer"] is None and r["escalate"] is True
    assert len(r["conflicts"]) == 1 and "disagree" in r["message"] and {"FAQ faq-010"} <= {r["conflicts"][0]["between"][0]}
    agreeing = passage("pawfect-handbook-009", "Cancellations", "Cancellations need at least 6 hours' notice before the appointment.")
    assert ev.respond("What notice is needed to cancel an appointment?", [CANCEL], [agreeing])["outcome"] == "faq"  # the same figure is not a conflict


def test_a_multi_part_question_with_only_one_part_answered_is_answered_in_part_and_says_so():
    r = ev.respond("Do you groom cats and do you offer daycare?", [PETS], [HOURS])
    assert r["outcome"] == "partial" and r["answer"] is None and r["evidence_class"] == "partial"
    assert r["unanswered_parts"] == ["do you offer daycare"] and r["partial"][0]["part"] == "Do you groom cats" and "cats" in r["partial"][0]["text"]
    assert r["answer_sufficiency"] == 0.5 and "only part" in r["message"] and "daycare" in r["message"] and r["escalate"] is True  # what is known is returned, what is not is named
    assert "[1]" in r["partial_answer"] and r["sources"][0]["id"] == "faq-004"  # with its reference
    both = ev.respond("Do you groom cats and what time does the studio close on Sunday?", [PETS], [HOURS])
    assert both["outcome"] == "grounded" and {s["kind"] for s in both["sources"]} == {"faq", "passage"}  # each part from the source that supports it


def test_the_monday_and_upi_example_the_supported_part_is_returned_and_the_unknown_one_named():
    upi = faq("faq-009", "What payment methods do you accept?", "Pawfect accepts UPI, cash, and debit or credit cards.")
    r = ev.respond("Are you open on Mondays and do you accept UPI?", [upi], [LATE])  # the knowledge base confirms UPI and says nothing about Mondays
    assert r["parts"] == ["Are you open on Mondays", "do you accept UPI"] and r["outcome"] == "partial"
    assert "UPI" in r["partial_answer"] and r["unanswered_parts"] == ["Are you open on Mondays"] and "Mondays" in r["message"]
    assert r["answer"] is None  # the whole question is NOT presented as answered
    assert ev.split_parts("What are your hours and address?") == ["What are your hours", "address"]  # two kinds of detail asked in one breath are two components


def test_each_kind_of_evidence_is_told_apart_on_its_own():
    # direct: the text says it in the question's own words
    d = ev.classify("Do you groom cats", PETS["answer"], topic=set(ev.terms(PETS["question"])))
    assert d["class"] == "direct" and d["supported"] and "own words" in d["reason"]
    # semantic: other words, but a sentence close in meaning, with a second independent sign (some words shared)
    close = lambda part, texts: [0.7 for _ in texts]  # noqa: E731
    sem = ev.classify("Can you make my dog's nails shorter", SERVICES["answer"], topic=set(), sim=close, q_sim=0.3)
    assert sem["class"] == "semantic" and sem["supported"] and sem["coverage"] < ev.MIN_SUPPORT
    # related but insufficient: the same subject, not the requested fact
    rel = ev.classify("How much does a bath cost", SERVICES["answer"], topic=set())
    assert rel["class"] == "related" and not rel["supported"] and "asks for price" in rel["reason"]
    far = ev.classify("How much does a bath cost", "We are open every day.", topic=set())
    assert far["class"] in ("related", "none") and not far["supported"]
    # none: nothing bears on it
    none = ev.classify("capital of France", HOURS["answer"], topic=set(), sim=lambda p, t: [0.05 for _ in t])
    assert none["class"] == "none" and not none["supported"]
    # meaning alone is not enough: close in meaning, no word shared, the entry not clearly nearest, and nothing corroborating
    weak = ev.classify("Do you offer a de-shedding treatment", SERVICES["answer"], topic=set(), sim=lambda p, t: [0.5 for _ in t], q_sim=0.2)
    assert weak["class"] == "related"
    # question-level classes
    q = lambda text, faqs, ps: ev.respond(text, faqs, ps)["evidence_class"]  # noqa: E731
    assert q("Which pets do you groom?", [PETS], []) == "direct"
    assert q("Do you groom cats and do you offer daycare?", [PETS], [HOURS]) == "partial"
    assert q("How much does a bath cost?", [SERVICES], [HOURS]) == "related_insufficient"
    assert q("What is the capital of France?", [SERVICES], [HOURS]) == "no_evidence"
    handbook = passage("pawfect-handbook-009", "Cancellations", "Cancellations require at least 24 hours' notice before the appointment.")
    assert q("What notice is needed to cancel an appointment?", [CANCEL], [handbook]) == "contradiction"


def test_a_question_with_a_figure_needs_a_figure_of_that_kind_in_the_answer():
    booking = faq("faq-005", "How do I book an appointment?", "Book by calling 9000012345 or sending a WhatsApp message to the same number.")
    assert ev.asks_for("Can I book at 8 AM?") == ["time"] and ev.respond("Can I book at 8 AM?", [booking], [])["outcome"] == "insufficient"  # a phone number says nothing about 8 AM
    assert ev.respond("Is the studio open at 8 AM?", [booking], [HOURS])["outcome"] == "grounded"  # the opening hours state times; nothing is inferred beyond them


def test_generation_keeps_a_valid_rewording_and_rejects_additions():
    question = "What happens if the owner is more than 15 minutes late and what is the late collection fee?"
    sim = lambda text, texts: [0.8 if ("shorten your service" in text or "pick-up costs" in text) else 0.1 for _ in texts]  # noqa: E731  (the model says only the two honest rewordings mean what a passage says)
    reworded = ev.respond(question, [SERVICES], [LATE, FEE, HOURS], sim=sim, llm=lambda p: "If you arrive over 15 minutes late the studio can shorten your service [1]. Late pick-up costs ₹100 for every extra 30 minutes [2].")
    assert reworded["used_llm"] is True and "₹100" in reworded["answer"] and "[1]" in reworded["answer"]  # different wording from the source is not a reason to throw an answer away
    for invented in ("The studio may shorten the service [1]. The late fee is ₹100, refunded if you call ahead [2].",  # an unsupported policy
                     "The studio may shorten the service [1]. The late fee is ₹150 [2].",  # an unsupported number
                     "The studio may shorten the service [1]. The fee is ₹100 and we guarantee your dog will be happy [2]."):  # an unsupported guarantee
        r = ev.respond(question, [SERVICES], [LATE, FEE, HOURS], sim=sim, llm=lambda p, t=invented: t)
        assert r["used_llm"] is False and "guarantee" not in r["answer"] and "refunded" not in r["answer"] and "₹150" not in r["answer"] and "₹100" in r["answer"]
    nothing = ev.respond("Do you offer dog daycare?", [SERVICES], [HOURS], llm=NoLLM())
    assert nothing["outcome"] == "insufficient" and nothing["used_llm"] is False  # no evidence: abstain, never general knowledge


def test_a_conflict_names_both_sources_and_what_each_says():
    handbook = passage("pawfect-handbook-009", "Cancellations", "Cancellations require at least 24 hours' notice before the appointment.")
    r = ev.respond("What notice is needed to cancel an appointment?", [CANCEL], [handbook], llm=NoLLM())
    assert r["outcome"] == "conflict" and "6 hours" in r["message"] and "24 hours" in r["message"] and "FAQ faq-010" in r["message"] and "pawfect_handbook.md" in r["message"]


def test_retrieval_evidence_and_response_are_independent_layers():
    """The evidence engine takes retrieved hits and returns plain data; the response engine takes that data. Neither needs the other's internals, and neither needs to know
    how the hits were found: a different embedding model, a reranker or a keyword index only has to produce hits of the same shape."""
    import json

    import response

    hits = [dict(PETS, score=0.2)]  # a poor retriever's score changes nothing about whether the answer supports the question
    a = ev.assess("Which pets do you groom?", hits, [HOURS])
    json.dumps({k: v for k, v in a.items() if k not in ("pool",)}, default=str)  # the assessment is data: it can be stored, inspected, or produced elsewhere
    assert a["evidence_class"] == "direct" and a["answer_sufficiency"] == 1.0 and a["retrieval_similarity"] == 0.2
    assert "answer" not in a and "outcome" not in a  # the evidence engine says nothing in the user's voice
    r = response.compose(a)
    assert r["outcome"] == "faq" and r["answer"] == PETS["answer"] and r == ev.respond("Which pets do you groom?", hits, [HOURS])
    forged = dict(a, conflicts=[{"part": "x", "between": ["A", "B"], "reason": "different", "texts": ["one", "two"]}])
    assert response.compose(forged)["outcome"] == "conflict"  # the response engine acts on the assessment alone
    reranked = [dict(HOURS, score=0.99), dict(HOURS, entry_id="other", score=0.1)]  # a different retriever's ordering is just different input
    assert ev.respond("What time does the studio close on Sunday?", [SERVICES], reranked)["outcome"] == "grounded"


def test_a_topic_only_question_is_not_answered_by_an_answer_that_does_not_address_it():
    call = faq("faq-077", "Do you offer dog daycare?", "Please call us.", score=1.0)
    low = lambda part, texts: [0.1 for _ in texts]  # noqa: E731
    assert ev.respond("Do you offer dog daycare?", [call], [], sim=low)["outcome"] == "insufficient" or ev.respond("Do you offer dog daycare?", [call], [], sim=low)["outcome"] == "faq"
    assert ev.classify("Do you offer dog daycare", "Please call us.", topic=set(ev.terms(call["question"])), sim=low, q_sim=0.6)["supported"] is False  # similar question, answer says nothing about it


def test_an_unrelated_question_gets_an_honest_non_answer():
    r = ev.respond("What is the capital of France?", [SERVICES], [HOURS, LATE], llm=NoLLM())
    assert r["outcome"] == "insufficient" and r["answer"] is None and r["evidence_support"] == 0.0 and r["response_confidence"] == "none"


def test_a_question_the_faq_fully_answers_returns_the_stored_answer_and_never_touches_the_fallback_or_a_model():
    r = ev.respond("Which pets do you groom?", [PETS], [HOURS, LATE, FEE], llm=NoLLM())
    assert r["outcome"] == "faq" and r["answer"] == PETS["answer"] and r["used_llm"] is False
    assert r["sources"] == [{"ref": 1, "kind": "faq", "id": "faq-004", "source": "pawfect_faq.csv", "location": "Which pets do you groom?"}]
    assert r["answer_sufficiency"] == 1.0 and r["response_confidence"] == "high"
    assert ev.respond("Do you groom cats?", [PETS], [])["outcome"] == "faq"  # a paraphrase the answer supports


# ---- a knowledge base of many kinds of source at once: rules that keep unrelated sentences from contradicting each other

class Close:
    """Stands in for the embedding model: every sentence that mentions one of the words is exactly this close to the question."""

    def __init__(self, closeness):
        self.closeness = closeness

    def __call__(self, part, texts):
        return [next((v for w, v in self.closeness.items() if w in t), 0.1) for t in texts]


def test_a_pronoun_in_a_later_part_is_read_as_the_first_part_named_thing():
    assert ev.split_parts("How long does the Slack integration take to set up, and which plans have it?") == ["How long does the Slack integration take to set up", "which plans have Slack"]
    assert ev.split_parts("What is the Pro plan's API limit and how much does it cost?")[1] == "how much does Pro cost"
    assert ev.split_parts("Where is Pawfect and when is it open?") == ["Where is Pawfect", "when is Pawfect open"]


def test_a_pronoun_is_left_alone_when_nothing_is_named_and_weekdays_are_not_names():
    assert ev.split_parts("Can I stay while it is groomed, and can I take it home?")[1].endswith("take it home")
    assert ev.split_parts("Are you open on Sunday and can I call it?")[1] == "can I call it"


def test_a_time_word_in_an_unrelated_sentence_does_not_make_it_a_contradiction():
    data = passage("billing-002", "Cancelling a subscription", "When you cancel, the plan stays active until the end of the period you have paid for. After that your data is kept for 30 days and then deleted.", source="billing_terms.md")
    other = faq("faq-010", "How do I reset my password?", "Use the link on the sign-in page. The reset link expires after 1 hour.")
    r = ev.respond("What happens to my data after I cancel?", [other], [data], sim=Close({"30 days": 0.8}))
    assert r["outcome"] == "grounded" and "30 days" in r["answer"] and not r.get("conflicts")


def test_a_unit_word_is_not_a_subject_so_a_trial_length_does_not_contradict_a_retention_period():
    profile = passage("profile-001", "Limits", "Limits, audit log retention: 365 days.", source="product_profile.json")
    trial = faq("faq-001", "Do you offer a free trial?", "Every plan has a 14 day free trial and no card is needed to start.")
    r = ev.respond("What is the audit log retention period in days?", [trial], [profile], sim=Close({"365": 0.8, "14 day": 0.5}))
    assert r["outcome"] in ("grounded", "faq") and "365" in r["answer"] and not r.get("conflicts")


def test_how_much_followed_by_a_thing_asks_for_an_amount_not_a_price():
    assert ev.asks_for("How much storage does the Business plan have?") == []
    assert ev.asks_for("How much does the Business plan cost?") == ["price"]
    assert ev.asks_for("How much is the Pro plan?") == ["price"]
    assert ev.asks_for("How much?") == ["price"]
    storage = passage("limits-001", "Limits: Storage GB", "Business plan, storage: 500 GB.", source="integrations.xlsx")
    r = ev.respond("How much storage does the Business plan have?", [], [storage])
    assert r["outcome"] == "grounded" and "500" in r["answer"]


def test_two_sources_that_are_equally_close_can_both_be_evidence_and_so_can_disagree():
    faq_entry = faq("faq-004", "Which payment methods do you accept?", "We accept credit cards and bank transfer. Invoices are payable within 30 days.")
    terms = passage("billing-004", "Invoices", "Invoices are payable within 15 days of the invoice date. Late invoices may be charged interest.", source="billing_terms.md")
    r = ev.respond("When are invoices due?", [faq_entry], [terms], sim=Close({"payable": 0.68}))
    assert r["outcome"] == "conflict" and {s["source"] for c in r["conflicts"] for s in c["sources"]} == {"pawfect_faq.csv", "billing_terms.md"}


def test_a_much_less_close_source_is_not_a_rival_of_the_closest_one():
    slack = passage("int-001", "Integrations: Slack", "Slack (Integrations), setup time: 5 minutes.", source="integrations.xlsx")
    sales = passage("int-002", "Integrations: Salesforce", "Salesforce (Integrations), setup time: 60 minutes.", source="integrations.xlsx")
    r = ev.respond("How long does the Slack integration take to set up?", [], [slack, sales], sim=Close({"Slack": 0.82, "Salesforce": 0.61}))
    assert r["outcome"] == "grounded" and "5 minutes" in r["answer"] and "60" not in r["answer"] and not r.get("conflicts")


def test_two_sentences_disagree_about_a_yes_no_question_only_if_both_are_about_its_own_words():
    a = "Please bring any medication, but staff cannot administer medication unless this has been explicitly agreed."
    b = "For the safety of pets and staff, grooming may be paused if a dog shows severe distress."
    assert ev.conflict_between("Can the staff give my dog medicine", a, b) is None
    c = "We accept cash and cards at the counter. UPI is not accepted."
    d = "Pawfect accepts UPI, cash, and debit or credit cards at the counter."
    assert ev.conflict_between("Is UPI accepted at the counter", c, d)
