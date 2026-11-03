"""Evidence-aware answering: a stored FAQ answer is returned as the answer only when it actually supports what was asked.

Similarity says two questions are alike. It does not say the stored answer contains the requested fact ("How much is a bath?" is very similar to
"What services do you offer?", whose answer lists a bath and no price), so similarity is kept apart from three other signals:

    retrieval_similarity   how alike the best FAQ question is (cosine, NOT a probability, never a confidence)
    answer_sufficiency     the share of the question's parts that ONE FAQ answer explicitly supports
    evidence_support       the share of parts supported by the evidence finally used (FAQ answers and/or document passages)
    response_confidence    a category (high / medium / low / none) from rules; it is not calibrated and says so

Each part of a question is judged against each candidate text and gets one of these classes (`classify`):
    direct    the text states it in the question's own words (and has the kind of detail asked for: a price, time, duration, place, contact, count)
    semantic  the text states it in other words: a sentence of it is close in meaning to the question, and a second, independent sign agrees (some of the
              question's words are in it, or the entry was itself retrieved as a close match). The kind of detail asked for must still be there
    related   the same subject, but the requested fact is not established (no price where a price was asked, words missing, only topic overlap)
    none      nothing in the text bears on it
The whole question then has an `evidence_class`: direct, semantic, partial (some parts supported), related_insufficient, contradiction or no_evidence.

Outcomes: faq (the stored answer, or stored answers, support every part) · grounded (every part supported, at least one only by document or web passages: the answer
is assembled from those passages' own sentences, with references) · partial (some parts supported: what is supported is returned, what is unknown is named) ·
insufficient (nothing available answers it) · conflict (sources disagree about the same thing: both are shown, none is chosen).

Standard library only; a model enters only as the optional `sim` function (how close a sentence is to a question part in meaning) and the optional `llm`. The checks are
explicit and inspectable, which also makes their limits plain: see LIMITS.
"""
from __future__ import annotations

import math
import re
from datetime import date

try:
    from . import temporal
    from .textutil import STOP, STOP_STEMS, SUFFIXES, stem
    from .textutil import terms as _terms
except ImportError:  # an exported package puts runtime/ on the path and imports the modules by their bare names
    import temporal
    from textutil import STOP, STOP_STEMS, SUFFIXES, stem
    from textutil import terms as _terms

# Words that mean the same thing in a business's own documents, read as one word WHEN JUDGING EVIDENCE (the keyword channel of retrieval does not use this). Deliberately short and
# general: each line is a synonym a customer and a document author commonly differ on, not a domain's vocabulary.
CANON = [(re.compile(p, re.I), w) for p, w in (
    (r"\bmoney[- ]back\b|\brefunds?\b|\brefunded\b|\brefunding\b|\breimburs\w*|\bpaid back\b", "refund"),
    (r"\bchief executive(?: officer)?\b|\bceo\b|\bmanaging director\b|\bleads\b|\bleader\b|\bleading\b|\bleads?(?= the\b)", "leader"),
    (r"\bhead ?quarters\b|\bhq\b|\bhead office\b|\bmain office\b", "headquarters"),
    (r"\bemployees?\b|\bstaff\b|\bpersonnel\b|\bworkforce\b|\bheadcount\b", "staff"),
    (r"\bany ?time\b|\bwhenever\b|\bat any point\b", "anytime"),
    (r"\bcancel\w*|\bterminat\w*|\bunsubscrib\w*|\bstop (?:my|the|your|our) (?:subscription|plan|membership|account|service)", "cancel"),
    (r"\breplies\b|\breply\b|\breplied\b|\brespond\w*|\banswers\b|\banswered\b|\banswering\b", "reply"),
    (r"\bbuy\w*|\bpurchas\w*|\bbought\b", "purchase"),
    (r"\bdiscount\w*|\brebates?\b|\bpromo\w*|\bcoupons?\b|\bvouchers?\b|\d+ ?% off|percent off|\bsav(?:e|es|ed|ing|ings)\b|half[- ]price", "discount"),
)]
FILLER = {stem(w) for w in "company business organisation organization firm already still currently actually really".split()}  # what a question calls the business itself: the knowledge base is about it, so the word says nothing about which sentence answers


def canonical(text: str) -> str:
    for pat, word in CANON:
        text = pat.sub(word, text)
    return text


def terms(text: str) -> list[str]:
    return _terms(canonical(text))

LIMITS = ("Sufficiency is judged from words, the kind of detail a question asks for (a price, a time, a duration, a place, a contact, a count) and, when a model is available, "
          "how close a sentence is to the question in meaning. It can miss a paraphrase that shares no words and sits far from the question in meaning, and can be satisfied by a "
          "sentence that is close in meaning but says something else. Conflicts are found for numbers with units and for yes/no statements about the same thing, not for every kind "
          "of disagreement. Several FAQ entries, or an entry and a passage, can support the same fact; which is cited is the strongest one.")

ASKS = {  # what kind of detail a part of the question wants, and how that detail looks when a text supplies it
    "price": (r"how much(?= (?:is|are|was|does|do|did|will|would|can|could|should|for|to|per|extra|more|less|it|that|this|they|these|those|money|cash)\b|\s*[?.!]?\s*$)|\bcost|\bprice|\bfees?\b|\bcharges?\b|\brates?\b|\bpriced?\b",
              r"(₹|\brs\.?|\$|€|£|\binr\b)\s?\d|\d+(?:\.\d+)?\s?(rupees?|dollars?|inr|usd|euros?|eur|pounds?|gbp)\b|\bfree\b|\bno (charge|fee|cost)\b|\bcomplimentary\b"),
    "duration": (r"how long|how many (minutes|hours|days|weeks|months|years)|\bduration\b|how soon|how quickly|how much time",
                 r"\d+\s?(to|-|–)?\s?\d*\s?(business |working |calendar )?(min|mins|minutes?|hours?|hrs?|days?|weeks?|months?|years?|yrs?)\b|\b(one|two|three|four|five|six|half) (hour|day|week|minute|month|year)s?\b|\ban hour\b"),
    "time": (r"what time|^\s*when\b|\bwhen (is|are|do|does|did|can|will|should|would)\b|\band when\b|opening hours|\bhours\b|closing|\bclose\b|which days|what days|\bschedule\b|are you open|is (it|the \w+) open|open (on|at|from|until|late|early)|\bopen days\b",
             r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b(mon|tues?|wednes|thurs?|fri|satur|sun)(day)?s?\b|\bclosed\b|\b24 ?(hours|hrs|/7)\b|\b\d{1,2}:\d{2}\b"
             r"|\b(19|20)\d{2}\b|\b\d{4}-\d{2}-\d{2}\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
             r"|\bwithin\s+\d+\s+(business |working |calendar )?(day|week|month|hour)s?\b|\bdue\b"
             r"|\b(at|upon|during|after|before|once|as soon as|every|each|whenever)\s+(?!the end)\w+"),
    "place": (r"\bwhere\b|\baddress\b|\blocation\b|\blocated\b|which (area|part|city|neighbourhood)|neighbourhood",
              r"\b(located|address|street|road|nagar|lane|floor|building|area|city|near)\b|\bin [A-Z][a-z]+"),
    "contact": (r"phone number|contact number|\bemail\b|how (can|do|should) i (contact|reach|call)|\bwhatsapp\b|\bcall (you|us)\b",
                r"\d{5,}|@|whatsapp|\+?\d[\d\s().-]{6,}\d"),
    "count": (r"how many\b(?! (minutes|hours|days|weeks|months|years))", r"\d+|\b(one|two|three|four|five|six|seven|eight|nine|ten|none)\b"),
}
CLAIMS = {  # what kind of business fact a question is about, and how a text states that kind of fact: a text about something else does not state it, however similar the sentences are
    "discount": (r"discount|promo|coupon|voucher|rebate|cheaper|\bsavings?\b|\bsave\b|special (price|rate|offer)|\bdeal\b|percent off|% off|\boff\b",
                 r"discount|promo|coupon|voucher|rebate|reduc|cheaper|savings?\b|\bsav(e|es|ed|ing)\b|% ?off|percent off|\d+ ?% (off|less)|half[- ]price|lower (price|rate)|\bless than the\b"),
    "refund": (r"refund|money back", r"refund|money back|reimburs|credited? back|paid back|all your money|full amount"),
    "trial": (r"\btrials?\b|try (it|before|for free)|\bdemo\b", r"\btrials?\b|try (it )?(out )?(for free|before)|\bdemo\b|sandbox|evaluat"),
    "guarantee": (r"guarantee|warrant(y|ies)|promise|commitment|\bsla\b|assure", r"guarantee|warrant(y|ies)|promise|commitment|committed|\bsla\b|assure|we ensure"),
    "eligibility": (r"eligib|qualif|entitled|who can (use|get|join|sign)", r"eligib|qualif|entitled|permitted|\bonly (for|to)\b|available (only )?to\b|must be|restricted to|open to|for (students|customers|members|schools|non-?profits)"),
    "exception": (r"exception|except\b|exclu|not covered|doesn.t apply", r"except|exception|excluding|excluded|not available|not covered|does not apply|do not apply|not refunded|not eligible|not included|cannot|can.t|unless|only if|once"),
    "condition": (r"\bconditions?\b|requirements?\b|\bunless\b|\bterms and\b", r"unless|only if|provided|subject to|requires?\b|must\b|conditions?\b|as long as|if you|once |when you|at least|within"),
}
CLAIM_RE = {k: (re.compile(q, re.I), re.compile(a, re.I)) for k, (q, a) in CLAIMS.items()}
ASK_WORDS = {stem(w) for w in "cost price fee fees charge charges rate rates area location located address phone email contact number whatsapp day days hour hours long soon quick quickly duration".split()}  # they name the detail asked for, which the kind check judges
ASK_RE = {k: (re.compile(q, re.I), re.compile(a, 0 if k == "place" else re.I)) for k, (q, a) in ASKS.items()}
NEGATIVE = re.compile(r"\b(no|not|never|cannot|can't|cant|don't|dont|doesn't|doesnt|won't|unable|only)\b", re.I)
QUANTITY = re.compile(r"(?P<money>(?:(?:₹|rs\.?|\$|€|£)\s?(?P<m>\d[\d,]*))|(?:\b(?P<m2>\d[\d,]*(?:\.\d+)?)\s?(?:usd|eur|gbp|inr|rupees?|dollars?|euros?|pounds?)\b))|(?P<time>\b(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s?(?P<ap>am|pm)\b)|(?P<dur>\b(?P<d>\d+)\s?(?:business |working |calendar )?(?P<u>min|mins|minutes?|hours?|hrs?|days?|weeks?|months?|years?|yrs?)\b)|(?P<pct>\b(?P<pv>\d+(?:\.\d+)?)\s?%)", re.I)
QUESTION_WORDS = r"(?:what|how|when|where|who|which|do|does|did|can|could|is|are|will|would|should|may)"
QUESTION_WORD_SET = {"what", "how", "when", "where", "who", "which", "do", "does", "did", "can", "could", "is", "are", "will", "would", "should", "may"}
MIN_SUPPORT = 0.75  # direct: this share of the words a part asks about are in the text ("explicitly": one missing word of two fails)
SEM_MIN = 0.45  # semantic: how close (cosine) a sentence must be to the part in meaning
SEM_COVER = 0.34  # ... together with this share of the words, or (with none of them) when it is clearly the closest candidate (see SEM_MARGIN) and
SEM_NO_WORDS = 0.10  # ... plus this much more when none of the question's words are in the text, and
SEM_WORDS = 0.60  # ... unless most of the question's words are in the text as well, and
SEM_MARGIN = 0.08  # ... closer to the question than every other entry's best sentence by this much, and
Q_MIN = 0.50  # ... the entry itself having been retrieved this close to the whole question (a second, independent sign: never enough alone)
PARAPHRASE_OF_OWN = 0.60  # ... and, when the answer also has the kind of detail asked for, this close is enough
OWN_QUESTION = 0.85  # a question this close to an entry's own question, asking for no particular detail, is that entry's question: its stored answer is what its author wrote for it
WINDOW_TIE = 0.03  # a sentence is preferred to two sentences unless the pair is better by more than this
RELATED = 0.30  # closeness in meaning from which a text counts as "related" rather than "nothing to do with it"
def present(term: str, have: set[str]) -> bool:
    """A word is in a text if its stem is, or if one stem begins the other (medic / medicin, treat / treatment, book / booking), so a different ending is not a different word."""
    return term in have or (len(term) >= 4 and any(len(h) >= 4 and (h.startswith(term) or term.startswith(h)) for h in have))


def asks_for(part: str) -> list[str]:
    """The kinds of detail a part wants: what it asks for ("how much", "what time"), and the kinds of figure it mentions ("at 8 AM" is about a time, "45 minutes" about a duration,
    "₹500" about a price): an answer that has no figure of that kind says nothing about it."""
    kinds = [k for k, (q, _) in ASK_RE.items() if q.search(part)]
    if "price" in kinds and re.match(r"\s*(do|does|did|is|are|was|were|can|could|will|would|should)\b", part, re.I) and not re.search(r"how much|\bcosts?\b|\bcharges?\b|\bfees?\b", part, re.I):
        kinds.remove("price")  # "Do the prices include VAT?" is about prices, it does not ask for one
    q = _quantities(part)
    for figure, kind in (("clock", "time"), ("money", "price"), ("duration", "duration")):
        if figure in q and kind not in kinds:
            kinds.append(kind)
    return kinds


def split_parts(question: str) -> list[str]:
    """The separate pieces of information a question asks for: "Are you open on Mondays and do you accept UPI?" is two; "What are your hours and address?" is two."""
    q = re.sub(r"\s+", " ", question.strip())
    out: list[str] = []
    for piece in (p.strip(" ,") for p in re.split(r"\?+|;", q)):
        for sub in re.split(rf"\s*(?:,|\band\b|&|\balso\b)\s+(?={QUESTION_WORDS}\b)", piece, flags=re.I):
            sub = sub.strip(" ,")
            halves = re.split(r"\s+(?:and|&)\s+", sub, maxsplit=1)
            if len(halves) == 2 and asks_for(halves[0]) and asks_for(halves[1]) and set(asks_for(halves[0])) != set(asks_for(halves[1])) and terms(halves[1]):
                out += [h.strip() for h in halves]  # two different kinds of detail asked for in one breath
            elif terms(sub):
                out.append(sub)
    return resolve_pronouns(out) or [q]


PRONOUN = re.compile(r"\b(?:it|its|them|they|their)\b", re.I)


def resolve_pronouns(parts: list[str]) -> list[str]:
    """"How long does the Slack integration take, and which plans have it?": the second part is about the same named thing, so it is judged with that name in place of "it". Only the first
    name (a run of capitalised words that does not start the question: "Slack", "Pro", "Pawfect Grooming Studio") is carried over; with none, the part stays as it was."""
    out = list(parts)
    for i in range(1, len(out)):
        if not PRONOUN.search(out[i]):
            continue
        rest = parts[i - 1].split(None, 1)[1] if len(parts[i - 1].split(None, 1)) == 2 else ""
        for m in re.finditer(r"\b[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*)*", rest):
            name = m.group(0)
            if name.lower() in NOT_NAMES or name.lower() in STOP or name == "I":
                continue
            out[i] = PRONOUN.sub(name, out[i])
            break
    return out


NOT_NAMES = {w for w in "monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september october november december".split()}


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


PRONOUN_START = re.compile(r"^(it|its|they|their|this|that|these|those|he|she|there|such|both|also|however|but|and)\b", re.I)


def windows(text: str, on=None) -> list[str]:
    """The pieces of a text that can support a part: each sentence (a sentence that starts with "It ..." only with the one before it, which says what "it" is) and each
    pair of neighbours, since an answer often says "open Tuesday to Sunday." and then "Closed on Mondays." A sentence that says it holds only before or after a date that is not `on`
    (the day the question is about) is left out, so "Until 30 June ... From 1 July ..." offers the one that holds."""
    sents = sentences(text) or [text]
    live = [True if on is None else temporal.in_force(x, on) is not False for x in sents]
    alone = [(s if not (i and PRONOUN_START.match(s)) else f"{sents[i - 1]} {s}", live[i] and (live[i - 1] if i and PRONOUN_START.match(s) else True)) for i, s in enumerate(sents)]
    out = [w for w, ok in alone if ok]
    out += [f"{x} {y}" for (i, (x, y)) in enumerate(zip(sents, sents[1:])) if live[i] and live[i + 1] and f"{x} {y}" not in out]
    return out


def common_terms(texts: list[str]) -> set[str]:
    """Words that are in at least half of the candidate texts: about this knowledge base's subject as a whole, so they say nothing about which entry answers."""
    if len(texts) < 6:
        return set()
    df: dict[str, int] = {}
    for t in texts:
        for w in set(terms(t)):
            df[w] = df.get(w, 0) + 1
    return {w for w, n in df.items() if n >= math.ceil(len(texts) / 2)}


def classify(part: str, text: str, topic: set[str] | None = None, sim=None, sem_min: float = SEM_MIN, q_sim: float = 0.0, common: set[str] | None = None, on=None, faq: bool = True, multi: bool = False) -> dict:
    """Judge one part of a question against one text: class (direct / semantic / related / none), and why."""
    kinds = asks_for(part)
    part = temporal.DATE.sub(" ", part)  # a date in the question sets the day it is about (see assess); it is not a word the answer has to repeat
    need = [t for t in dict.fromkeys(terms(part)) if not (topic and t in topic) and not (common and t in common) and not t.isdigit() and not (kinds and t in ASK_WORDS)]  # numbers describe the asker's situation; ask words are judged by the kind check
    if any(t not in FILLER and not t.isdigit() for t in terms(part)):  # "the company" names the business, which every sentence is about: it is not a word an answer must repeat, unless it is all that was asked
        need = [t for t in need if t not in FILLER]
    claims = [k for k, (q, _) in CLAIM_RE.items() if q.search(part)]
    scoped = [(n, x) for n, x in re.findall(r"\b([A-Z][\w-]+)\s+(plans?|tiers?|customers?|users?|accounts?|editions?)\b", part)]
    wins = windows(text, on) if text else []
    sems = sim(part, wins) if (sim is not None and wins) else [None] * len(wins)
    own_terms = [t for t in dict.fromkeys(terms(part)) if not t.isdigit() and not (common and t in common)]
    topic_cover = (sum(t in topic for t in own_terms) / len(own_terms)) if (topic and own_terms) else 0.0  # how much of the part is the entry's own question's words
    everything = [t for t in dict.fromkeys(terms(part)) if not t.isdigit() and not (kinds and t in ASK_WORDS) and not (common and t in common)]  # the subject's words too: repeating them counts as evidence
    asked = [t for t in dict.fromkeys(terms(part)) if kinds and t in ASK_WORDS]
    best = None
    for w, s in zip(wins, sems):
        have = set(terms(w))
        for n, x in scoped:  # "the Team plan": a sentence about all plans is about it too
            if re.search(rf"\b(?:all|every|each|any)\s+(?:\w+\s+)?{re.escape(x.lower().rstrip('s'))}", w.lower()):
                have.add(stem(n))
        cover = (sum(present(t, have) for t in need) / len(need)) if need else 1.0
        repeats = (sum(present(t, have) for t in everything) / len(everything)) if everything else 0.0
        kind_ok = all(ASK_RE[k][1].search(w) for k in kinds) and all(CLAIM_RE[k][1].search(w) for k in claims)  # the kind of fact asked about must be the kind the sentence states
        named = (sum(present(t, have) for t in asked) / len(asked)) if asked else 0.0  # the sentence uses the very word asked for ("address:"), not only something shaped like it
        key = (kind_ok, round(cover + repeats * 0.01 + named * 0.1 + (s or 0.0), 4))  # the right kind of detail first, then the closest in words and meaning; a shorter window wins a tie
        if best is None or key[0] > best[0][0] or (key[0] == best[0][0] and key[1] > best[0][1] + WINDOW_TIE):  # a longer window (listed after the shorter ones) must be clearly better, not just a hair
            best = (key, w, cover, kind_ok, s, [t for t in need if not present(t, have)], repeats)
    if best is None:
        return {"class": "none", "supported": False, "coverage": 0.0, "semantic": None, "detail_ok": False, "sentence": "", "missing": need, "asks_for": kinds, "reason": "there is no text"}
    _, sentence, cover, kind_ok, s, missing, repeats = best
    out = {"coverage": round(cover, 2), "semantic": None if s is None else round(s, 2), "detail_ok": kind_ok, "sentence": sentence, "missing": missing, "asks_for": kinds, "need": need}
    # in a question of several parts, a passage found only because its HEADING is a word the part borrowed from another ("Which plans include SSO, and do they come with a dedicated manager?")
    # has not been found for what is asked about it: the closeness of the part to the heading is not a second sign when none of the other words are in the text
    topical_only = multi and (not faq) and cover == 0 and bool(need) and topic_cover > 0
    corroborated = cover >= SEM_COVER or (q_sim >= Q_MIN and not topical_only)
    if not kind_ok:
        missing_claims = [k for k in claims if not CLAIM_RE[k][1].search(sentence)]
        why = (f"it asks about {', '.join(missing_claims)} and the text does not state one: it speaks of something else" if missing_claims else f"it asks for {', '.join(kinds)} and the text states none")
        cls = "related" if (cover > 0 or (s or 0) >= RELATED) else "none"
    elif need and cover >= MIN_SUPPORT:
        cls, why = "direct", "the text states it in the question's own words"
    elif not need and repeats >= MIN_SUPPORT:
        cls, why = "direct", "the question names the entry's subject and the answer repeats its words"
    elif not need and kinds and (q_sim >= PARAPHRASE_OF_OWN or topic_cover >= MIN_SUPPORT):
        cls, why = "semantic", "the answer has the kind of detail asked for, and the question is a close paraphrase of the entry's own question"
    elif not need and (s is None or q_sim >= OWN_QUESTION):
        cls, why = "direct", ("the question is the entry's own question and asks for no particular detail" if s is not None else
                              "the question only repeats the entry's own subject (no model was available to check the answer addresses it)")
    elif not need and s >= sem_min and q_sim >= Q_MIN:
        cls, why = "semantic", f"the question names the entry's subject and the answer is close to it in meaning ({s:.2f})"
    elif s is not None and s >= (sem_min if cover >= SEM_COVER else sem_min + SEM_NO_WORDS) and corroborated:
        cls, why = "semantic", f"a sentence is close in meaning to the question ({s:.2f}) and {'shares its words' if cover >= SEM_COVER else 'the entry matched the question closely'}"
    elif need:
        cls = "related" if (cover > 0 or (s or 0) >= RELATED) else "none"
        why = f"words missing: {', '.join(missing)}" + ("" if s is None else f"; closest sentence is only {s:.2f} in meaning")
    else:
        cls = "related" if (s or 0) >= RELATED else "none"
        why = "the question only names the entry's subject and the answer is not close enough to it in meaning" + ("" if s is None else f" ({s:.2f})")
    return {**out, "class": cls, "supported": cls in ("direct", "semantic"), "reason": why}


GENERIC = {stem(w) for w in "take need require get make give work start happen apply provide include mean usually typically generally after before during until within about day days hour hours minute minutes week weeks month months year years".split()}  # verbs, time words and units that say nothing about WHICH thing a sentence is about


def shares_subject(g: dict) -> bool:
    """Does a text that is only 'related' speak of the question's subject (a word that names the thing, not a verb like 'takes') and not just share the kind of figure? 'Delivery takes
    1 to 2 days' is not about repairs, so it cannot contradict 'a repair takes 2 to 3 days'."""
    specific = [t for t in g.get("need", []) if t not in GENERIC]
    return not specific or sum(t not in g["missing"] for t in specific) / len(specific) >= 0.5 and any(t not in g["missing"] for t in specific)


def _topic(hit: dict) -> set[str]:
    if hit.get("kind", "faq") == "faq":
        return set(terms(hit.get("question", ""))) | (set(terms(hit.get("matched", ""))) if hit.get("matched_kind", "q") == "q" else set())
    return set(terms(hit.get("question", "")))  # a passage's heading names its subject


def assess_faq(question: str, hit: dict, sim=None, sem_min: float = SEM_MIN, common: set[str] | None = None) -> dict:
    """Does this FAQ answer explicitly support every part of the question? Independent of how similar the question is: a similarity of 1.00 changes nothing here."""
    topic = _topic(hit)
    parts = []
    for part in split_parts(question):
        a = classify(part, hit.get("answer", ""), topic, sim, sem_min, float(hit.get("score", 0.0)), common)
        parts.append({"part": part, **a, "supported": bool(hit.get("answer")) and a["supported"]})
    n = len(parts)
    return {"parts": parts, "sufficiency": sum(p["supported"] for p in parts) / n, "sufficient": all(p["supported"] for p in parts)}


def _quantities(text: str) -> dict[str, set[float]]:
    """Numbers with units in a sentence, by kind: money, clock times, durations (in minutes)."""
    out: dict[str, set[float]] = {}
    for m in QUANTITY.finditer(text):
        if m.group("money"):
            out.setdefault("money", set()).add(float((m.group("m") or m.group("m2")).replace(",", "")))
        elif m.group("pct"):
            out.setdefault("percent", set()).add(float(m.group("pv")))
        elif m.group("time"):
            h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
            out.setdefault("clock", set()).add(h * 60 + int(m.group("mi") or 0))
        else:
            unit = m.group("u").lower()
            out.setdefault("duration", set()).add(float(m.group("d")) * (60 if unit.startswith("h") else 1440 if unit.startswith("d") else 10080 if unit.startswith("w") else 43200 if unit.startswith("mo") else 525600 if unit.startswith("y") else 1))
    return out


SCOPE = re.compile(r"\b(?:(?P<name>[A-Z][\w-]+)|(?P<kind>annual|monthly|yearly|student|students|non-?profit|new|existing|business|enterprise|free|paid|trial))\s+(?:plans?|tiers?|customers?|users?|accounts?|subscriptions?|editions?|members?)\b")
UNIVERSAL = re.compile(r"\b(?:all|every|each|any)\s+(?:\w+\s+)?(?:plans?|tiers?|customers?|users?|accounts?|editions?)\b", re.I)
OPPOSITES = [("include", "exclude"), ("includes", "excludes"), ("included", "excluded"), ("including", "excluding"), ("available", "unavailable"), ("allowed", "prohibited"), ("allowed", "forbidden"),
             ("accepted", "rejected"), ("open", "closed"), ("free", "chargeable"), ("free", "charged"), ("refundable", "non-refundable"), ("refundable", "nonrefundable"), ("supported", "unsupported"),
             ("before", "after")]


def scopes(sentence: str) -> set[str]:
    """Who or what a sentence is about when it says: "Team plan: ...", "Annual plans can ...", "for students", or all of them. Two sentences about different scopes are about different
    things, whatever words they share."""
    out = {(m.group("name") or m.group("kind")).lower() for m in SCOPE.finditer(sentence)}
    out.discard("customers")
    if UNIVERSAL.search(sentence):
        out.add("all")
    return out


def opposite(a: str, b: str) -> bool:
    wa, wb = set(re.findall(r"[a-z-]+", a.lower())), set(re.findall(r"[a-z-]+", b.lower()))
    return any((x in wa and y in wb) or (y in wa and x in wb) for x, y in OPPOSITES)


def conflict_between(part: str, a: str, b: str, common: set[str] | None = None) -> str | None:
    """A reason when two sentences about the same thing disagree: different amounts of the same kind, or one says no where the other does not. They must be about the same thing as EACH
    OTHER (not only each about the question), and about the same scope: "Team plan: 24 hours" and "Growth plan: 8 hours" are different plans, not a contradiction."""
    sa, sb = scopes(a), scopes(b)
    if sa and sb and "all" not in sa | sb and not (sa & sb):
        return None  # different products, customers or kinds of plan
    both = (set(terms(a)) & set(terms(b))) - GENERIC - (common or set())
    if not {t for t in both if not t.isdigit()}:
        return None  # nothing in common but the figures
    qa, qb = _quantities(a), _quantities(b)
    for kind in qa.keys() & qb.keys():
        if qa[kind] and qb[kind] and not (qa[kind] & qb[kind]):
            return f"one says {sorted(qa[kind])} and the other {sorted(qb[kind])} ({kind})"
    first = part.split()[0].lower() if part.split() else ""
    if not asks_for(part) and first in ("do", "does", "can", "is", "are", "will", "may", "could", "would"):
        ta, tb = set(terms(a)), set(terms(b))
        shared = ta & tb & set(terms(part))
        core = [t for t in terms(part) if not t.isdigit() and t not in ASK_WORDS and t not in FILLER and t not in (common or ())]  # what this question is distinctly about, not the subject every passage shares
        enough = min(2, len(core))  # at least two of the question's own words, or the one it has
        same_thing = sum(present(t, ta) for t in core) >= enough and sum(present(t, tb) for t in core) >= enough  # two sentences disagree about it only if both are about it
        differ = bool(NEGATIVE.search(a)) != bool(NEGATIVE.search(b)) or opposite(a, b)
        if len(shared) >= 2 and same_thing and differ and not re.search(r"\bonly\b", a + " " + b, re.I):
            return "one says yes where the other says no"
    return None


def locate(hit: dict) -> str:
    """Where a passage is: its file or address, its section, and which piece of it."""
    ordinal = re.search(r"-(\d+)$", hit.get("entry_id", hit.get("id", "")))
    return f"{hit.get('source') or 'document'} › {hit.get('question', '')}" + (f" (passage {int(ordinal.group(1))})" if ordinal else "")


def _ref(hit: dict) -> dict:
    kind = hit.get("kind", "passage")
    return {"kind": kind, "id": hit["entry_id"], "source": hit.get("source") or ("FAQ" if kind == "faq" else "document"), "location": hit.get("question", "") if kind == "faq" else locate(hit)}


def _label(h: dict) -> str:
    return f"FAQ {h['entry_id']}" if h.get("kind", "faq") == "faq" else locate(h)


def assess(question: str, faq_hits: list[dict], passage_hits: list[dict], sim=None, sem_min: float = SEM_MIN, unknown_names: list[str] | None = None, on=None, outdated: set[str] | None = None, multi: bool | None = None) -> dict:
    """THE EVIDENCE ENGINE. Does the retrieved material support an answer? Takes what a retriever found (`faq_hits` and `passage_hits`, best first, each
    {entry_id, question, answer, score, matched, matched_kind, source, kind}) and returns an assessment, plain data and nothing else: what the question's parts are, how
    every part fares against every candidate (direct / semantic / related / none, with the reason), which candidates support each part, the first conflict if sources
    disagree, and the separate signals. It writes no answer: that is the response engine's job (response.compose), and it does not care how the candidates were found."""
    parts = split_parts(question)
    day = temporal.reference_day(question, on or date.today())  # a sentence that holds only on other days than the one asked about is not evidence for it
    pool = list(faq_hits) + list(passage_hits)  # every candidate the retrieval found, of every kind: nothing is cut because of what kind it is
    common = common_terms([f"{h.get('question', '')} {h.get('answer', '')}" for h in pool])
    top_sim = float(faq_hits[0]["score"]) if faq_hits else 0.0
    n_faq = len(faq_hits)
    own = [sim(part, [h.get("question", "") for h in pool]) if (sim is not None and pool) else [0.0] * len(pool) for part in parts]  # how close each PART is to each entry's own question or heading

    def closeness(p: int, i: int) -> float:  # the entry having been retrieved as close to what is asked: for a question in several parts, to THIS part
        h = pool[i]
        return max(float(h.get("score", 0.0)) if len(parts) == 1 else 0.0, own[p][i] if (i < n_faq or len(parts) > 1) else float(h.get("score", 0.0)))

    grid = [[classify(part, h.get("answer", ""), _topic(h), sim, sem_min, closeness(p, i), common, day, h.get("kind", "faq") == "faq", (len(parts) > 1) if multi is None else multi) for i, h in enumerate(pool)] for p, part in enumerate(parts)]  # every part against every candidate
    for p, part in enumerate(parts):  # a name the knowledge never mentions: no sentence, however close in meaning, can be about it
        words = {stem(w) for w in re.findall(r"[^\W_]+", part)}
        named = [n for n in (unknown_names or []) if n in words]
        if named:
            for i, g in enumerate(grid[p]):
                if g["class"] in ("direct", "semantic"):
                    grid[p][i] = {**g, "class": "related", "supported": False, "reason": f"the question names {', '.join(named)}, which nothing in the knowledge mentions"}
    for p, part in enumerate(parts):  # out-of-date material states it in full and the current material only comes near: the current material does not say it
        if outdated and part in outdated:
            for i, g in enumerate(grid[p]):
                if g["class"] in ("direct", "semantic") and g["coverage"] < 1.0:
                    grid[p][i] = {**g, "class": "related", "supported": False, "reason": "material that is no longer current states this fully; the current material does not"}
    for p in range(len(parts)):  # a meaning-only match counts only when it stands out: and a direct statement beats it
        has_direct = any(g["class"] == "direct" for g in grid[p])
        semantic = {j for j, h in enumerate(grid[p]) if h["class"] == "semantic"}
        for i, g in enumerate(grid[p]):
            if g["class"] != "semantic":
                continue
            # other entries that are just as plausible an answer (semantic too, and within the margin) may agree (corroboration) or disagree (a conflict): they are not what this one must stand out from
            others = [h["semantic"] for j, h in enumerate(grid[p]) if j != i and h["semantic"] is not None and pool[j]["entry_id"] != pool[i]["entry_id"]
                      and not (j in semantic and abs(h["semantic"] - (g["semantic"] or 0.0)) < SEM_MARGIN)]
            stands_out = g["coverage"] >= SEM_WORDS or (g["semantic"] or 0.0) - max(others, default=0.0) >= SEM_MARGIN
            if has_direct or not stands_out:
                grid[p][i] = {**g, "class": "related", "supported": False, "reason": ("a direct statement exists elsewhere" if has_direct else "close in meaning, but not clearly closer than other entries' sentences")}
    faq_scores = [(sum(grid[p][i]["supported"] for p in range(len(parts))) / len(parts), -i) for i in range(n_faq)]
    best_i = max(range(n_faq), key=lambda i: faq_scores[i]) if n_faq else None

    def strength(p: int, i: int) -> tuple:
        g = grid[p][i]
        # a direct statement first; then how fully the sentence covers what was asked and how close it is in meaning; the retrieval ranking and the entry's closeness only break ties,
        # so a stored FAQ answer wins a part by what it says, not by being a FAQ entry (a question sits nearer to another question than to a sentence of a document)
        return (g["class"] == "direct", round(g["coverage"], 1), round(g["semantic"] or 0.0, 2), round(float(pool[i].get("combined_score", pool[i].get("score", 0.0))), 2), round(closeness(p, i), 2))

    support = [sorted((i for i in range(len(pool)) if grid[p][i]["supported"]), key=lambda i: strength(p, i), reverse=True) for p in range(len(parts))]
    conflicts = []
    for p in range(len(parts)):
        if not support[p]:
            continue
        lead = support[p][0]
        near = [i for i in range(len(pool)) if i not in support[p] and grid[p][i]["class"] == "related" and grid[p][i]["detail_ok"] and grid[p][i]["coverage"] >= 0.5 and shares_subject(grid[p][i])]
        for i in support[p][1:] + near:  # sources that support it, and ones that speak of the same thing closely enough to contradict it
            if pool[i]["entry_id"] == pool[lead]["entry_id"]:
                continue
            ta, tb = grid[p][lead]["sentence"], grid[p][i]["sentence"]
            why = conflict_between(parts[p], ta, tb, common)
            if not why:  # the best sentence of each need not be the ones that disagree: look at the other sentences of each that are about what was asked
                kt = [t for t in dict.fromkeys(terms(parts[p])) if t not in GENERIC and t not in FILLER and t not in ASK_WORDS and not t.isdigit() and t not in (common or ())]
                near = lambda x: any(present(t, set(terms(x))) for t in kt)  # noqa: E731
                mine = [x for x in sentences(pool[lead].get("answer", "")) if near(x)][:3]
                theirs = [x for x in sentences(pool[i].get("answer", "")) if near(x)][:3]
                for xa in mine:
                    for xb in theirs:
                        why = why or conflict_between(parts[p], xa, xb, common)
                        if why:
                            ta, tb = xa, xb
            if why:
                conflicts.append({"part": parts[p], "between": [_label(pool[lead]), _label(pool[i])], "reason": why, "texts": [ta, tb],
                                  "sources": [{**_ref(pool[lead]), "text": ta}, {**_ref(pool[i]), "text": tb}]})
                break
    chosen = [(p, support[p][0]) for p in range(len(parts)) if support[p]]
    unanswered = [parts[p] for p in range(len(parts)) if not support[p]]
    related = any(grid[p][i]["class"] == "related" for p in range(len(parts)) if not support[p] for i in range(len(pool)))
    suff = faq_scores[best_i][0] if best_i is not None else 0.0
    cls = ("contradiction" if conflicts else "partial" if chosen and unanswered else "related_insufficient" if unanswered and related else "no_evidence" if unanswered else
           "semantic" if any(grid[p][i]["class"] == "semantic" for p, i in chosen) else "direct")
    return {"question": question, "parts": parts, "pool": pool, "n_faq": n_faq, "common": common, "grid": grid, "support": support, "chosen": chosen, "unanswered": unanswered, "conflicts": conflicts,
            "best_faq": best_i, "evidence_class": cls, "retrieval_similarity": top_sim, "retrieval_similarity_note": "cosine similarity of the best FAQ question: not a probability",
            "answer_sufficiency": suff, "evidence_support": len(chosen) / len(parts), "faq_considered": faq_hits[best_i]["entry_id"] if best_i is not None else None, "limits": LIMITS,
            "part_assessments": [{"part": parts[p], "against": {pool[i]["entry_id"]: {k: grid[p][i][k] for k in ("class", "coverage", "semantic", "reason")} for i in range(len(pool))
                                                              if grid[p][i]["class"] != "none"}} for p in range(len(parts))]}


def respond(question: str, faq_hits: list[dict], passage_hits: list[dict], llm=None, similarity_floor: float = 0.0, sim=None, sem_min: float = SEM_MIN, unknown_names: list[str] | None = None, on=None) -> dict:
    """Assess, then compose: the evidence engine (here) and the response engine (response.py) back to back. Either can be replaced on its own."""
    try:
        from . import response
    except ImportError:
        import response
    return response.compose(assess(question, faq_hits, passage_hits, sim, sem_min, unknown_names, on), llm=llm, similarity_floor=similarity_floor, sim=sim)
