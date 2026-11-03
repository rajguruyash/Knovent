"""Builds examples/washo/: the improved FAQ (cleaned entries plus alternate phrasings), the independent typed held-out set,
and the map from the previous FAQ's entries. This file is the source of truth; the CSVs are its output.

    python scripts/build_washo_data.py

Rules the data follows, and the checks at the bottom enforce:
  * no new facts: every price and phone number in an improved answer already appears in the previous FAQ, and no alternate
    phrasing contains a number, or a word like discount, free, UPI, booking, that is not in the FAQ;
  * the held-out questions are written separately from the training phrasings, then checked for near-duplicates of them;
  * merges happen only where the previous answers carried the same information (see CHANGES in the output).
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faqengine.heldout import assign_splits, load_heldout, near_duplicate  # noqa: E402
from faqengine.kb import load_kb  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "examples" / "washo"

# id, category, question, answer, previous entry numbers (faq-NNN), alternate phrasings
ENTRIES = [
    ("services", "services", "What services does WASHO offer?",
     "WASHO offers doorstep bike and car washing: bike wash, car body wash, car deep cleaning and SUV deep cleaning, as well as membership plans.",
     [1],
     ["what all do you offer", "list of WASHO services", "what kind of washing services are available", "which services are available with you",
      "tell me about your services", "services offered by washo", "wat services do u provide", "what can you wash and clean for me",
      "doorstep bike and car washing services"]),
    ("bike-wash", "pricing", "Do you wash bikes, and how much does a bike wash cost?",
     "Yes. WASHO offers doorstep bike washing for ₹65.", [2, 3],
     ["bike wash price", "how much to wash my bike", "what do you charge for washing a bike", "do you clean bikes at home", "bike washing rate",
      "how mch is bike wsh", "bike wash cost pls", "is bike washing available and what is the fee", "is a bike wash ₹65"]),
    ("bike-and-car-price", "pricing", "How much does it cost to wash a bike and a car together?",
     "A bike wash costs ₹65 and a car body wash costs ₹150.", [25],
     ["price for washing both my bike and my car", "bike plus car wash charges", "I have a bike and a car, what will it cost", "cost to wash bike and car",
      "what do I pay for one bike wash and one car body wash", "bike n car wash price", "how much for bike & car both",
      "washing two vehicles a bike and a car cost", "bike and car wsh price"]),
    ("car-wash", "services", "Do you wash cars?",
     "Yes. WASHO offers doorstep car body washing and deep cleaning.", [4],
     ["do you do car washing", "is car cleaning available", "can you wash my car at home", "do you offer car wash services", "car wash available?",
      "which car services do you have", "do u wash cars", "can I get my car cleaned by you", "car body wash and deep cleaning available"]),
    ("car-body-wash-price", "pricing", "What is the price of a car body wash?",
     "A car body wash costs ₹150.", [5],
     ["car body wash cost", "how much to wash a car", "what do you charge for a car wash", "car wash rate", "price for exterior car wash",
      "how much is car bodywash", "car wsh price", "cost of washing my car", "is a car body wash ₹150"]),
    ("car-deep-cleaning-price", "pricing", "How much does car deep cleaning cost?",
     "Car deep cleaning costs ₹220. SUV deep cleaning is priced separately at ₹250.", [6],
     ["car deep cleaning price", "deep clean cost for a car", "how much for deep cleaning my car", "charges for deep cleaning of cars",
      "car deep cleening rate", "what is the price of a deep clean for a car", "deep cleaning car fee", "car deep clean ₹220?",
      "how much is a deep wash for a car"]),
    ("suv-deep-cleaning", "pricing", "Do you clean SUVs, and what does SUV deep cleaning cost?",
     "Yes. WASHO offers SUV deep cleaning for ₹250. Car deep cleaning costs ₹220.", [7, 8, 26],
     ["SUV deep cleaning price", "do you wash SUVs", "how much to deep clean an SUV", "is SUV cleaning available", "suv cleening cost",
      "what do you charge for SUV deep cleaning", "can you clean my SUV at home", "SUV services", "does washo do suv", "is SUV deep cleaning ₹250"]),
    ("body-vs-deep", "pricing", "What is the difference between a car body wash and deep cleaning, and how do their prices compare?",
     "A car body wash is the standard exterior washing service and costs ₹150. Deep cleaning is a separate, more extensive service and costs ₹220 for a car.", [23, 24],
     ["body wash vs deep cleaning", "which is better a car body wash or deep cleaning", "how is deep cleaning different from a body wash",
      "difference between car wash and deep clean", "body wash or deep cleaning price difference", "deep clean vs bodywash cost",
      "is deep cleaning costlier than a body wash", "compare car body wash and deep cleaning", "boday wash vs deep cleaning",
      "standard exterior wash and extensive deep cleaning difference"]),
    ("doorstep-area", "area", "Do you provide doorstep washing, where do you operate, and how do I check my address is covered?",
     "Yes. WASHO provides doorstep car and bike washing services in Kharadi, Pune. The service is carried out at your location in its service area, "
     "so you do not need to take your vehicle to a washing centre. To confirm whether service is available at your address or apartment, "
     "contact WASHO with your location.", [9, 10, 11, 27, 28],
     ["where do you operate", "which areas do you serve", "do you come to my place to wash", "do I have to bring my vehicle to a washing centre",
      "is doorstep service available in Pune", "can you wash my car at my apartment", "how do I check if my address is covered", "is my area covered",
      "will you come to my society", "do u provide doorstep wash in kharadi pune"]),
    ("car-wash-process", "process", "How is a car body wash performed?",
     "The wash process includes pressure washing, applying foam, spreading the foam with a cloth, rinsing and drying the vehicle with a cloth.", [12],
     ["what steps are involved in a car body wash", "how do you wash a car", "explain your car wash process", "what does the car wash include", "wash process",
      "do you use pressure washing", "how is the car cleaned step by step", "how do u wash the car", "what happens during a car body wash",
      "process of car body wsh"]),
    ("foam", "process", "Do you use foam while washing cars?",
     "Yes. WASHO uses foam as part of its car washing process.", [13],
     ["is foam used for car wash", "do you apply foam", "foam wash available?", "do you do foam wash", "does the car wash have foam", "foam used or not",
      "do u use foam", "is it a foam wash", "foam car wsh?"]),
    ("drying", "process", "Do you dry the car after washing?",
     "Yes. The vehicle is dried with a cloth after rinsing.", [14],
     ["is the car dried after wash", "do you wipe the car dry", "how do you dry the vehicle", "do you dry with a cloth", "drying after rinsing?",
      "will the car be dry when you finish", "do u dry the car", "is drying included", "car dry after wash?"]),
    ("interior-and-plans", "membership", "Is interior cleaning included, and how do the Basic and Pro plans differ?",
     "Interior vacuum cleaning is included according to the applicable membership plan. The Basic plan includes it once a month, while the Pro plan includes it four times a month.",
     [15],
     ["is interior cleaning included", "do you vacuum the inside of the car", "difference between basic and pro plan", "basic vs pro membership",
      "what do basic and pro plans include", "is interior vacuuming part of the membership", "interior vacuum cleaning included?",
      "compare basic and pro plans", "inside cleaning of car included", "wat is the diffrence in basic and pro"]),
    ("basic-vacuum", "membership", "How often is interior vacuum cleaning included in the Basic plan?",
     "The Basic plan includes interior vacuum cleaning once a month. The Pro plan includes it four times a month.", [16],
     ["basic plan vacuum cleaning", "how many vacuum cleanings in the basic plan", "basic membership interior cleaning frequency",
      "how often do I get interior vacuuming on basic", "basic plan once a month vacuum?", "vaccum cleaning in basic plan", "basic plan interior cleaning",
      "does the basic plan include vacuum cleaning", "in basic how many times is the interior cleaned"]),
    ("pro-vacuum", "membership", "How often is interior vacuum cleaning included in the Pro plan?",
     "The Pro plan includes interior vacuum cleaning four times a month. The Basic plan includes it once a month.", [17],
     ["pro plan vacuum cleaning", "how many vacuum cleanings in the pro plan", "pro membership interior cleaning frequency",
      "how often do I get interior vacuuming on pro", "pro plan four times a month vacuum?", "vaccum cleaning in pro plan", "pro plan interior cleaning",
      "does the pro plan include vacuum cleaning", "in pro how many times is the interior cleaned"]),
    ("custom-membership", "membership", "Can I get a customized membership or a custom price for multiple washes?",
     "Yes. WASHO offers customized membership plans based on the washing frequency and service combination you need. "
     "Contact the team with your preferred services and washing frequency to discuss an appropriate plan.", [18, 29],
     ["can I get a custom membership plan", "do you offer customised plans", "custom price for multiple washes", "I want a plan made for me",
      "can the membership be tailored to my needs", "custom price for regular washes", "customized membership possible?", "personalised membership",
      "custom plan for several washes a month", "can I get a customised wash package"]),
    ("weekly-frequency", "membership", "Can I choose how many washes I need per week?",
     "WASHO offers membership options based on different weekly washing frequencies, and customized plans can be discussed.", [19],
     ["how many times a week can I get washing", "weekly wash frequency options", "can I pick the number of washes per week",
      "weekly washing frequency in membership", "can I decide how often my vehicle is washed each week", "wash frequency options per week",
      "can I have a wash every week", "how often can I get my vehicle washed in a week", "washes per week choose?"]),
    ("combine-washes", "membership", "Can I combine different types of washes in a membership?",
     "Yes. WASHO can create customized combinations of services, such as body washes and deep cleaning.", [20],
     ["can I mix body wash and deep cleaning in a plan", "combination of services in a membership", "can I get body washes and deep cleaning together",
      "customised combo of washes", "can a membership include different services", "mix and match services in membership",
      "body wash plus deep cleaning in the same plan", "combine services", "can I add deep cleaning to my membership", "different wash types in one plan"]),
    ("become-member", "membership", "How can I become a WASHO member?",
     "Contact WASHO to discuss the membership plan that suits your vehicle and preferred washing frequency.", [21],
     ["how do I join washo membership", "how to take a membership", "membership sign up", "I want to become a member", "how can I subscribe to a plan",
      "how do I get a membership for my car", "how to join as member", "membership process", "how can I enrol for a membership",
      "wat do i do to get membership"]),
    ("one-time-wash", "pricing", "Can I ask for a one-time wash instead of a membership?",
     "WASHO has individual service prices: bike wash ₹65, car body wash ₹150, car deep cleaning ₹220 and SUV deep cleaning ₹250.", [22],
     ["can I get just one wash", "do I need a membership for a single wash", "one time wash without membership", "what are your prices", "price list",
      "single wash price", "can I pay per wash", "do you have individual service prices", "only one time wash needed", "how much do you charge for each service"]),
    ("contact", "contact", "How can I contact WASHO?",
     "You can contact WASHO at 866990147 or 9172792929.", [30],
     ["what is your phone number", "contact number", "how do I reach you", "how to call washo", "phone number pls", "how can I get in touch with washo",
      "customer care number", "mobile number", "washo contact details", "number to call"]),
]

# Held-out questions, written separately from the phrasings above. P paraphrase, A answer-terms, T typo, S short/incomplete.
CODE = {"P": "paraphrase", "A": "answer-terms", "T": "typo", "S": "short"}
HELD = {
    "services": [("P", "hi, could you tell me what kinds of cleaning you do?"), ("P", "I'd like to know what WASHO can do for my vehicles"),
                 ("A", "washing, deep cleaning and membership plans, is that everything you provide"), ("T", "what servces r offerd"),
                 ("S", "services"), ("S", "your offerings")],
    "bike-wash": [("P", "how much will it set me back to get my bike washed"), ("P", "do you guys also clean bikes or just cars"),
                  ("A", "is the doorstep bike washing charge 65 rupees"), ("T", "bike wasing prise"), ("S", "bike wash?"), ("S", "bike rate")],
    "bike-and-car-price": [("P", "I want my bike and my car both washed, what's the total charge"), ("P", "how much for washing one bike and one car in the same visit"),
                           ("A", "₹65 bike and ₹150 car, is that what both cost"), ("T", "how mch for washng my bike and car both"), ("S", "bike + car price"),
                           ("S", "both vehicles cost")],
    "car-wash": [("P", "can you take care of my car's washing too"), ("P", "I need my car cleaned, is that something you do"),
                 ("A", "do you handle car body washing and deep cleaning"), ("T", "is cleening of my car posible with u"), ("S", "car wash?"), ("S", "car washing service")],
    "car-body-wash-price": [("P", "what would I pay for a regular wash of my car"), ("P", "how much do you take for washing a car's exterior"),
                            ("A", "does a regular car wash come to 150 rupees"), ("T", "car bodi wash prise"), ("S", "body wash price"), ("S", "car wash fee")],
    "car-deep-cleaning-price": [("P", "how much is it for a thorough cleaning of my car"), ("P", "price of getting my car deep cleaned"),
                                ("A", "is car deep cleaning 220 rupees"), ("T", "deep cleening car cost"), ("S", "deep cleaning cost car"), ("S", "car deep wash")],
    "suv-deep-cleaning": [("P", "my vehicle is an SUV, can you clean it and what does that cost"), ("P", "do you take SUVs too"),
                          ("A", "SUV deep cleaning for 250, is it available"), ("T", "wat will u charg for cleening my suvv"), ("S", "SUV rate"), ("S", "suv wash")],
    "body-vs-deep": [("P", "what's the point of paying more for deep cleaning over the normal wash"), ("P", "deep cleaning costs how much more than a body wash"),
                     ("A", "standard exterior wash versus the more extensive service"), ("T", "diffrence btween bodywash and deep cleening"),
                     ("S", "deep clean or body wash"), ("S", "body wash deep clean difference")],
    "doorstep-area": [("P", "do you come to us or do we have to drop the vehicle off"), ("P", "which part of the city do you cover"),
                      ("A", "is the service in Kharadi only"), ("T", "do you opertae in pune"), ("S", "doorstep?"), ("S", "where are you located"),
                      ("P", "I stay in an apartment building, will your team come there"), ("P", "how can I tell whether you serve my locality"),
                      ("A", "should I contact washo with my location to confirm service"), ("T", "how can i confrim ur servce at my adress"),
                      ("S", "serve my area?"), ("S", "can you reach my house"), ("S", "areas covered"), ("P", "do you come to my apartment"),
                      ("P", "do you wash in Baner")],
    "car-wash-process": [("P", "walk me through what happens when you wash a car"), ("P", "what exactly do you do to the car during the wash"),
                         ("A", "pressure wash, foam, rinse and drying with cloth, is that the process"), ("T", "how is the car wahsed"),
                         ("S", "washing steps"), ("S", "wash procedure")],
    "foam": [("P", "is there any foam in the way you wash cars"), ("P", "I want to know if you use foam on my car"),
             ("A", "foam is part of the car washing process right"), ("T", "is there foamm in ur wash"), ("S", "foam?"), ("S", "any foam")],
    "drying": [("P", "after rinsing, how do you get the vehicle dry"), ("P", "once it is rinsed do you finish it off dry"),
               ("A", "is a cloth used for drying the vehicle after rinsing"), ("T", "will it be dryed afer rinsng"), ("S", "drying?"), ("S", "dried with cloth")],
    "interior-and-plans": [("P", "does a membership cover cleaning the inside of the car"), ("P", "what is the difference in benefits between your two membership plans"),
                           ("A", "interior vacuum cleaning according to the applicable membership plan"), ("T", "basci and pro plan diffrence"),
                           ("S", "pro or basic"), ("S", "plans difference")],
    "basic-vacuum": [("P", "on the basic membership how many times is the car vacuumed"), ("P", "what do I get for interior vacuuming if I choose Basic"),
                     ("A", "basic members get their interior vacuumed once monthly, correct"), ("T", "basic plan vacum frequncy"), ("S", "basic vacuum"), ("S", "basic interior")],
    "pro-vacuum": [("P", "on the pro membership how many times is the car vacuumed"), ("P", "what do I get for interior vacuuming if I choose Pro"),
                   ("A", "pro members get their interior vacuumed four times monthly, correct"), ("T", "pro plan vacum frequncy"), ("S", "pro vacuum"), ("S", "pro interior")],
    "custom-membership": [("P", "I wash my car often, can you make a plan just for me"), ("P", "is it possible to get a membership that fits my own schedule"),
                          ("A", "customized membership plans based on frequency and service combination, do you have that"), ("T", "can u mak a plan accordng to my need"),
                          ("S", "custom plan"), ("S", "tailored membership")],
    "weekly-frequency": [("P", "can I decide how many times each week my vehicle gets washed"), ("P", "do the plans let me choose weekly washes"),
                         ("A", "membership options based on different weekly washing frequencies"), ("T", "washes per wek can i choos"),
                         ("S", "number of washes weekly"), ("S", "weekly frequency")],
    "combine-washes": [("P", "can one plan have both a body wash and a deep clean"), ("P", "I want different kinds of services in the same membership"),
                       ("A", "customized combinations of services such as body washes and deep cleaning"), ("T", "can i comine services in membrship"),
                       ("S", "service combos"), ("S", "mix services")],
    "become-member": [("P", "I'm interested in a membership, what should I do next"), ("P", "how do I go about signing up for a plan"),
                      ("A", "contact washo to discuss the membership plan suiting my vehicle and washing frequency"), ("T", "how to becom a membr"),
                      ("S", "join membership"), ("S", "get membership")],
    "one-time-wash": [("P", "I don't want a subscription, just a single wash, is that okay"), ("P", "can I just pay for one service without joining a plan"),
                      ("A", "individual service prices for bike wash, car body wash, car deep cleaning and SUV deep cleaning"),
                      ("T", "no subscripton, just a singl wash plz"), ("S", "rate card"), ("S", "no membership")],
    "contact": [("P", "how can I speak to someone at washo"), ("P", "who do I call to ask something"), ("A", "866990147 is that your number?"),
                ("T", "how do i reech sombody on phone"), ("S", "phone"), ("S", "call washo")],
}
AMBIGUOUS = [
    ("how much does deep wash cost", ["car-deep-cleaning-price", "suv-deep-cleaning", "body-vs-deep"], "owner"),
    ("how much for a deep clean", ["car-deep-cleaning-price", "suv-deep-cleaning", "body-vs-deep"], ""),
    ("SUV or car deep cleaning which costs more", ["car-deep-cleaning-price", "suv-deep-cleaning"], ""),
    ("how much do you charge", ["one-time-wash", "bike-wash", "car-body-wash-price", "car-deep-cleaning-price", "suv-deep-cleaning", "bike-and-car-price"], ""),
    ("what are the rates", ["one-time-wash", "bike-wash", "car-body-wash-price", "car-deep-cleaning-price", "suv-deep-cleaning", "bike-and-car-price"], ""),
    ("what is the cost of cleaning cars", ["car-body-wash-price", "car-deep-cleaning-price", "body-vs-deep"], ""),
    ("price of washing", ["one-time-wash", "bike-wash", "car-body-wash-price"], ""),
    ("wash price for my vehicles", ["bike-wash", "car-body-wash-price", "bike-and-car-price", "one-time-wash"], ""),
    ("membership", ["custom-membership", "become-member", "weekly-frequency", "combine-washes", "interior-and-plans"], ""),
    ("plan details", ["interior-and-plans", "basic-vacuum", "pro-vacuum", "custom-membership"], ""),
    ("vacuum cleaning", ["interior-and-plans", "basic-vacuum", "pro-vacuum"], ""),
    ("interior", ["interior-and-plans", "basic-vacuum", "pro-vacuum"], ""),
    ("how is the wash done", ["car-wash-process", "foam", "drying"], ""),
    ("do you wash both bikes and cars", ["bike-wash", "car-wash", "services"], ""),
]
OOS_NEAR = [
    ("any offers", "owner"), ("do you accept UPI payments", ""), ("what are your working hours", ""), ("can I book a slot for tomorrow morning", ""),
    ("do you give discounts for regular customers", ""), ("how long does a wash take", ""), ("do you wash buses or trucks", ""), ("can you wash my bicycle", ""),
    ("do you do ceramic coating or polishing", ""), ("what shampoo or chemicals do you use", ""), ("do you bring your own water", ""),
    ("are you open on Sundays", ""), ("can I cancel my membership", ""), ("how much does the membership cost per month", ""),
    ("what is the price of the Basic plan", ""), ("do you clean the engine bay", ""), ("is there a refund if I am not happy with the wash", ""),
    ("how do I pay", ""),
]
OOS_FAR = [
    "what is the weather today", "who won the cricket match last night", "tell me a joke", "what is the capital of India", "how do I reset my phone",
    "recommend a good restaurant in Pune", "what is two plus two", "how do I cook rice", "best movies to watch this weekend", "how is the stock market doing",
    "how do I learn python", "translate hello to Hindi", "who is the prime minister", "how far is the moon", "write me a poem about the rain",
    "what is the best phone to buy", "how many days are in a leap year", "can you help me with my homework",
]

FORBIDDEN_IN_ALTS = {"discount", "deal", "free", "cheap", "coupon", "promo", "cashback", "upi", "cash", "card", "online", "app", "booking", "slot", "appointment",
                     "today", "tomorrow", "sunday", "sedan", "hatchback", "bicycle", "cycle", "scooter", "truck", "bus", "hour", "hours", "24", "7"}


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def build() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    prev = load_kb(OUT / "faq.previous.csv")
    prev_text = " ".join(e.answer for e in prev.entries)
    errors: list[str] = []

    # the improved FAQ
    seen_ids, all_alts = set(), {}
    with open(OUT / "faq.improved.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "question", "answer", "category", "alt_questions"])
        for eid, cat, q, a, _prev, alts in ENTRIES:
            assert eid not in seen_ids, eid
            seen_ids.add(eid)
            if not 5 <= len(alts) <= 10:
                errors.append(f"{eid}: {len(alts)} alternate phrasings (want 5 to 10)")
            if numbers(a) - numbers(prev_text):
                errors.append(f"{eid}: answer has numbers not in the previous FAQ: {numbers(a) - numbers(prev_text)}")
            for alt in alts:
                if "|" in alt or alt.lower() in all_alts:
                    errors.append(f"{eid}: duplicate or bad phrasing {alt!r} (also in {all_alts.get(alt.lower())})")
                all_alts[alt.lower()] = eid
                if numbers(alt) - numbers(a) - numbers(q):
                    errors.append(f"{eid}: phrasing {alt!r} contains a number the entry does not")
                words = set(re.findall(r"[a-z0-9]+", alt.lower()))
                if words & FORBIDDEN_IN_ALTS:
                    errors.append(f"{eid}: phrasing {alt!r} uses {sorted(words & FORBIDDEN_IN_ALTS)}, which the FAQ does not support")
            w.writerow([eid, q, a, cat, "|".join(alts)])
    # every previous entry is accounted for
    covered = sorted(n for e in ENTRIES for n in e[4])
    if covered != list(range(1, len(prev) + 1)):
        errors.append(f"previous entries not all mapped exactly once: {covered}")
    with open(OUT / "previous_map.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "previous"])
        for e in ENTRIES:
            w.writerow([e[0], "|".join(f"faq-{n:03d}" for n in e[4])])

    # the held-out set
    rows = []
    for eid, items in HELD.items():
        assert eid in seen_ids, eid
        rows += [{"query": t, "entry_id": eid, "type": CODE[c], "source": "owner" if t in ("services", "body wash price") else ""} for c, t in items]
    rows += [{"query": q, "entry_id": "|".join(ids), "type": "ambiguous", "source": src} for q, ids, src in AMBIGUOUS]
    rows += [{"query": q, "entry_id": "", "type": "out-of-scope-near", "source": src} for q, src in OOS_NEAR]
    rows += [{"query": q, "entry_id": "", "type": "out-of-scope-far", "source": ""} for q in OOS_FAR]
    for r in rows:
        for x in r["entry_id"].split("|"):
            if x and x not in seen_ids:
                errors.append(f"held-out {r['query']!r}: unknown entry {x}")
    assign_splits(rows)
    with open(OUT / "heldout.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["query", "entry_id", "type", "split", "source"])
        w.writeheader()
        w.writerows(rows)

    # leakage: nothing in the held-out set may be a near-duplicate of a training phrasing
    improved = load_kb(OUT / "faq.improved.csv")
    ho = load_heldout(OUT / "heldout.csv", improved)
    for r in ho.removed:
        errors.append(f"LEAK: held-out {r['query']!r} ({r['type']}) is a near-duplicate of training phrasing {r['matches_training_phrasing']!r}")
    for w_ in ho.warnings:
        errors.append(f"warning: {w_}")
    print(f"improved FAQ: {len(improved)} entries, {sum(len(e.questions) - 1 for e in improved.entries)} alternate phrasings "
          f"({min(len(e.questions) - 1 for e in improved.entries)} to {max(len(e.questions) - 1 for e in improved.entries)} per entry)")
    c = ho.counts()
    print(f"held-out: {c['total']} questions ({c['by_split']['dev']} dev, {c['by_split']['test']} test); by type:")
    for t, d in c["by_type"].items():
        print(f"   {t:20s} dev {d['dev']:3d}  test {d['test']:3d}")
    if errors:
        print("\nPROBLEMS:")
        print("\n".join("  - " + e for e in errors))
        raise SystemExit(1)
    print("data checks passed")


if __name__ == "__main__":
    build()
