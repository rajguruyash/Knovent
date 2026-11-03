"""The verification layer: a worded answer is checked against its evidence claim by claim. Each case says whether the answer must pass."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "faqengine" / "runtime"))
import verify as v  # noqa: E402

LATE = "If an owner is more than 15 minutes late, the studio may shorten the service or offer the next available appointment."
FEE = "A late collection fee of ₹100 applies for each additional 30-minute period."
LATE_Q = "What happens if the owner is more than 15 minutes late and what is the late collection fee?"
REFUND = "Refunds are available only if the item is unused and returned within 7 business days. Approved refunds are processed within 7 business days."
HOURS = "The studio is open Tuesday to Sunday, 9:00 AM to 7:00 PM. It is closed on Mondays."
PETS = "Pawfect currently accepts dogs only. It does not groom cats or other animals."
BATH = "A bath costs ₹600 for small dogs and takes 45 minutes."
CANCEL = "Please give at least 6 hours' notice to cancel or reschedule an appointment."
MEANING = lambda text, texts: [0.8 if ("shorten your service" in text or "pick-up costs" in text) else 0.1 for _ in texts]  # noqa: E731  (a stand-in for the embedding model)

CASES = [  # (name, answer, [evidence], mode, question, passes, issue type that must be among the findings)
    ("extract", "The studio may shorten the service [1]. A late collection fee of ₹100 applies for each additional 30-minute period [2].", [LATE, FEE], "grounded", LATE_Q, True, None),
    ("valid reword", "If you arrive over 15 minutes late the studio can shorten your service [1]. Late pick-up costs ₹100 for every extra 30 minutes [2].", [LATE, FEE], "grounded", LATE_Q, True, None),
    ("unsupported policy", "The studio may shorten the service [1]. The late fee is ₹100, refunded if you call ahead [2].", [LATE, FEE], "grounded", LATE_Q, False, "unsupported_claim"),
    ("changed price", "The studio may shorten the service [1]. The late fee is ₹150 [2].", [LATE, FEE], "grounded", LATE_Q, False, "changed_number"),
    ("added guarantee", "The studio may shorten the service [1]. The fee is ₹100 and we guarantee your dog will be happy [2].", [LATE, FEE], "grounded", LATE_Q, False, "misrepresented_policy"),
    ("changed unit", "A late collection fee of ₹100 applies for each additional 30-hour period [2].", [LATE, FEE], "grounded", LATE_Q, False, "changed_number"),
    ("planted link", "Visit http://evil.example/pay to confirm.", [REFUND], "grounded", "", False, "added_detail"),
    ("planted e-mail", "Send your card number to refunds@evil.example.", [REFUND], "grounded", "", False, "added_detail"),
    ("planted phone", "Call 9876543210 now.", [REFUND], "grounded", "", False, "added_detail"),
    ("invalid reference", "The studio may shorten the service [3].", [LATE], "grounded", "", False, "bad_reference"),
    ("condition kept", "Refunds are available only if the item is unused and returned within 7 business days.", [REFUND], "grounded", "", True, None),
    ("condition dropped", "Refunds are available and are processed within 7 business days.", [REFUND], "grounded", "Do you offer refunds?", False, "missing_condition"),
    ("business days become days", "Refunds are processed within 7 days.", [REFUND], "grounded", "how long do refunds take", False, "changed_number"),
    ("number as a word", "Approved refunds are processed within seven business days.", [REFUND], "grounded", "", True, None),
    ("policy flipped", "Refunds are not available if the item is unused.", [REFUND], "grounded", "", False, "contradiction"),
    ("hours", "We are open Tuesday to Sunday from 9:00 AM to 7:00 PM and closed on Mondays.", [HOURS], "grounded", "", True, None),
    ("closing time changed", "We are open Tuesday to Sunday from 9:00 AM to 8:00 PM.", [HOURS], "grounded", "", False, "changed_number"),
    ("closed day made open", "We are open on Monday from 9:00 AM to 7:00 PM.", [HOURS], "grounded", "", False, "changed_number"),
    ("open day inside a range", "We are open on Wednesday.", [HOURS], "grounded", "", True, None),
    ("open day made closed", "We are closed on Wednesdays.", [HOURS], "grounded", "", False, "changed_number"),
    ("closed day kept", "We are closed on Mondays.", [HOURS], "grounded", "", True, None),
    ("percentage changed", "The deposit is 30% of the price.", ["A deposit of 20% is required at booking."], "grounded", "", False, "changed_number"),
    ("date changed", "Offer ends on 15 March.", ["The offer ends on 10 March."], "grounded", "", False, "changed_number"),
    ("notice hours changed", "Please give at least 12 hours notice.", [CANCEL], "grounded", "", False, "changed_number"),
    ("notice kept", "Please give at least 6 hours notice.", [CANCEL], "grounded", "Can I cancel?", True, None),
    ("free invented", "Parking is free.", ["Parking costs ₹50 per hour."], "grounded", "", False, "misrepresented_policy"),
    ("no charge is free", "Parking is free of charge.", ["There is no charge for parking."], "grounded", "", True, None),
    ("polish keeps figures", "A bath for a small dog costs ₹600 and takes 45 minutes.", [BATH], "polish", "", True, None),
    ("polish drops figures", "We give a bath.", [BATH], "polish", "", False, "dropped_detail"),
    ("polish adds an animal", "We groom dogs and cats.", [PETS], "polish", "", False, "contradiction"),
    ("polish with a pleasantry", "Sure! We only groom dogs.", [PETS], "polish", "", True, None),
    ("empty", "", [REFUND], "grounded", "", False, "empty"),
    ("opening yes turned into no", "No. Appointments are required; walk-ins are not accepted.", ["Yes. Appointments are required; walk-ins are not accepted."], "polish", "", False, "contradiction"),
    ("a dropped not in a short clause", "Dogs are welcome on the outdoor terrace but inside.", ["Dogs are welcome on the outdoor terrace but not inside."], "polish", "", False, "contradiction"),
    ("the same mixed sentence is fine", "Owners may stay for the handover, but they cannot remain in the grooming area.", ["Owners may stay for the handover, but they cannot remain in the grooming area."], "polish", "", True, None),
    ("a year given as a decade", "Leonardo painted the Mona Lisa in the early 3001s.", ["Leonardo painted the Mona Lisa in the early 1500s."], "polish", "", False, "changed_number"),
    ("a rate in another unit", "The cheetah can run at about 100 kilometres per minute.", ["The cheetah can run at about 100 kilometres per hour."], "polish", "", False, "changed_number"),
    ("the word only dropped", "Pawfect currently accepts dogs.", ["Pawfect currently accepts dogs only."], "polish", "", False, "missing_condition"),
    ("only kept in other words", "Pawfect currently accepts just dogs.", ["Pawfect currently accepts dogs only."], "polish", "", True, None),
    ("days after a time on a full stop", "We are open Monday to Friday from 8am to 6pm and Saturday from 9am to 1pm. The clinic is closed on Sundays.",
     ["We are open Monday to Friday from 8am to 6pm and Saturday from 9am to 1pm. The clinic is closed on Sundays."], "polish", "", True, None),
]


@pytest.mark.parametrize("name,answer,evidence,mode,question,passes,issue", CASES, ids=[c[0] for c in CASES])
def test_verification_cases(name, answer, evidence, mode, question, passes, issue):
    r = v.verify(answer, [{"ref": i + 1, "text": t} for i, t in enumerate(evidence)], mode, question, MEANING)
    assert r["passed"] is passes, r["issues"]
    if issue:
        assert issue in {i["type"] for i in r["issues"]}, r["issues"]
    assert r["limits"] and all({"type", "claim", "detail", "ref"} <= set(i) for i in r["issues"])


def test_meaning_alone_is_never_proof():
    r = v.verify("Pets are welcome and parking is plentiful.", [{"ref": 1, "text": "Pawfect currently accepts dogs only."}], "grounded", "", lambda t, ts: [0.99 for _ in ts])
    assert r["passed"] is False and "unsupported_claim" in {i["type"] for i in r["issues"]}  # a model that says "very close" still needs the words and facts to agree


def test_each_claim_gets_a_status_and_a_rejected_one_says_why():
    r = v.verify("The studio may shorten the service [1]. The late fee is ₹150 [2].", [{"ref": 1, "text": LATE}, {"ref": 2, "text": FEE}], "grounded", LATE_Q, MEANING)
    assert [c["status"] for c in r["claims"]] == ["supported", "rejected"] and "changed_number" in r["claims"][1]["issues"]
