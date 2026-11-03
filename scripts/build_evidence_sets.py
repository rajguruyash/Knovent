"""Writes the evidence test sets for examples/evidence (a dog groomer's FAQ and handbook, plus a policy update that contradicts them on two points).

Each row was written by reading the three knowledge files and stating what a careful person would say: the expected outcome, the facts the answer must
contain, the facts it must not contain, and which entries must be cited. The rows are then split by a hash of their id into three DISJOINT sets:

    dev.csv         looked at freely, to find and fix failures
    regression.csv  a deployment regression suite: checked before a new version is released (so it influences release decisions)
    locked.csv      the final benchmark: never looked at while building, run once at the end, reported only (its sha1 is in locked.sha1)

    python scripts/build_evidence_sets.py
"""
import csv
import hashlib
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "examples" / "evidence"
# id | category | expected outcome | question | must contain (;) | must not contain (;) | sources that must be cited (;)
ROWS = """
d1|direct|faq|Which pets do you groom?|dogs only||faq-004
d2|direct|faq|Where is Pawfect Grooming Studio located?|Viman Nagar||faq-002
d3|direct|faq|How do I book an appointment?|9000012345||faq-005
d4|direct|faq|How long does a grooming session take?|60 to 90 minutes||faq-007
d5|direct|grounded|What time does the studio open and close?|9:00 AM;7:00 PM||pawfect-handbook-007
d6|direct|grounded|Is the studio closed on Mondays?|closed on Mondays||pawfect-handbook-007
d7|direct|grounded|What fee applies for late collection?|₹100||pawfect-handbook-003
d8|direct|faq|Do you provide home visits?|No||faq-003
d9|direct|faq|Do I need an appointment?|walk-ins are not accepted||faq-006
d10|direct|grounded|Is grooming paused if a dog is very distressed?|paused||pawfect-handbook-004
p1|paraphrase|faq|Can you clean my dog's ears?|ear cleaning||faq-001
p2|paraphrase|faq|Do you take cats?|dogs only||faq-004
p3|paraphrase|faq|Which area of Pune are you in?|Viman Nagar||faq-002
p4|paraphrase|faq|How can I reserve a slot?|9000012345||faq-005
p5|paraphrase|grounded|What is the latest time I can start an appointment?|5:30 PM||pawfect-handbook-007
p6|paraphrase|grounded|My dog has a skin allergy, should I mention it?|allergies||pawfect-handbook-001
p7|paraphrase|grounded|How long can I wait before collecting my dog?|30 minutes||pawfect-handbook-003
p8|paraphrase|grounded|Do you use a muzzle?|only when necessary||pawfect-handbook-004
p9|paraphrase|grounded|Are your grooming tools cleaned between dogs?|cleaned grooming tools||pawfect-handbook-005
p10|paraphrase|grounded|Can the staff give my dog medicine?|cannot administer medication||pawfect-handbook-002
p11|paraphrase|grounded|Will you take photos of my dog?|permission||pawfect-handbook-006
p12|paraphrase|faq|Is it okay to stay in the room while my dog is groomed?|cannot remain in the grooming area||faq-008
p13|paraphrase|grounded|Do dogs need to be on a leash when they arrive?|leash||pawfect-handbook-002
p14|paraphrase|faq|Is it possible to turn up without booking?|walk-ins are not accepted||faq-006
pa1|partial|partial|Do you groom cats and do you offer teeth brushing?|dogs only|teeth brushing is|faq-004
pa2|partial|partial|Are you open on Mondays and do you accept Amex cards?|closed on Mondays|Amex cards are|pawfect-handbook-007
pa3|partial|partial|What are your opening hours and do you offer puppy discounts?|9:00 AM||pawfect-handbook-007
pa4|partial|partial|Where are you located and how much does a full groom cost?|Viman Nagar|₹|faq-002
pa5|partial|partial|Can I stay during grooming and do you have a waiting lounge?|cannot remain in the grooming area||faq-008
pa6|partial|partial|Is the studio open on Sundays and do you groom rabbits?|Tuesday to Sunday|rabbits are|pawfect-handbook-007
r1|related|insufficient|How much does a bath cost?||₹|
r2|related|insufficient|Do you offer dog daycare?||daycare is|
r3|related|insufficient|Which shampoo brands do you use?||brand|
r4|related|insufficient|Do you groom large breeds like Great Danes?||Great Dane|
r5|related|insufficient|How many groomers work at the studio?||groomers work|
r6|related|insufficient|Is there a fee for cancelling an appointment?||fee of|
r7|related|insufficient|Do you give discounts to regular customers?||discount of|
r8|related|insufficient|What time is the last pick-up of the day?||last pick-up is|
r9|related|insufficient|Do you offer a de-shedding treatment?||de-shedding|
r10|related|insufficient|Can I bring my puppy before its first vaccinations?||vaccin|
u1|unsupported|insufficient|What is the capital of France?||Paris|
u2|unsupported|insufficient|Who won the IPL match yesterday?||
u3|unsupported|insufficient|How do I fix a flat bicycle tyre?||
u4|unsupported|insufficient|What is the weather in Pune today?||
u5|unsupported|insufficient|Can you recommend a good veterinarian?||
u6|unsupported|insufficient|How do I invest in mutual funds?||
m1|multi-part|faq|Where are you located and how do I book?|Viman Nagar;9000012345||faq-002;faq-005
m2|multi-part|grounded|Which pets do you groom and what days are you open?|dogs only;Tuesday to Sunday||faq-004;pawfect-handbook-007
m3|multi-part|grounded|What happens if I am more than 15 minutes late and what is the late collection fee?|shorten;₹100||pawfect-handbook-003
m4|multi-part|grounded|Do you groom cats and are you open on Sunday?|dogs only;Sunday||faq-004;pawfect-handbook-007
m5|multi-part|faq|How long is a session and can I stay with my dog?|60 to 90 minutes;cannot remain||faq-007;faq-008
m6|multi-part|grounded|Are towels washed between dogs and may I take photos at pick-up?|Towels are washed;permission||pawfect-handbook-005;pawfect-handbook-006
c1|conflict|conflict|What notice is needed to cancel an appointment?|||
c2|conflict|conflict|Do you accept UPI payments?|||
c3|conflict|conflict|Can I reschedule an appointment at short notice?|||
c4|conflict|conflict|What is the minimum notice for rescheduling?|||
c5|conflict|conflict|Is UPI accepted at the counter?|||
"""


def main() -> None:
    rows = [r.split("|") for r in ROWS.strip().splitlines()]
    sets = {"dev": [], "regression": [], "locked": []}
    for r in rows:
        bucket = int(hashlib.md5(r[0].encode()).hexdigest(), 16) % 10
        sets["dev" if bucket < 3 else "regression" if bucket == 3 else "locked"].append(r)
    for name, part in sets.items():
        with (OUT / f"{name}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["id", "category", "expected_outcome", "question", "must_contain", "must_not_contain", "cite"])
            w.writerows(part)
    (OUT / "locked.sha1").write_text(hashlib.sha1((OUT / "locked.csv").read_bytes()).hexdigest() + "\n")
    for name, part in sets.items():
        cats = {}
        for r in part:
            cats[r[1]] = cats.get(r[1], 0) + 1
        print(f"{name:11s} {len(part):3d} {cats}")


if __name__ == "__main__":
    main()
