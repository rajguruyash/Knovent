"""THE VERIFICATION LAYER. Checks a worded answer (a model's, or any other) against the evidence it was made from, claim by claim, before anyone sees it. It is separate from the
response engine (which composes) and from the evidence engine (which decides what is supported): it decides only whether a given text stays inside given evidence.

For each sentence of the answer (a claim) it finds the evidence sentences that carry it and compares typed facts, not just words:
    figures      prices (with currency), clock times, durations (in minutes; business days apart from calendar days), percentages, dates, weekdays (ranges expanded), plain numbers
    contacts     web addresses, e-mail addresses, phone numbers: an answer must not bring any that the evidence does not have (the usual way a planted instruction gets its link out)
    polarity     a statement that turns a yes into a no, or the reverse; and opposites (free/charged, open/closed, refundable/non-refundable, before/after ...)
    commitments  guarantees, "always", "never", "unlimited", "free" ... that the evidence does not make; and money, refund or cancellation words it does not mention
    support      most of the claim's words are in the evidence sentences, or (with a model to measure meaning) a close meaning plus 40% of the words: never meaning alone, and every figure is still compared exactly
    conditions   an "if", "unless", "only", "must", "at least" ... of a sentence the answer relies on must still be in the answer (or in the customer's own question)
    details      (rewording a stored answer) every figure of the original must survive
    references   [n] must point at a real source

Any issue fails the answer. The caller then falls back to the stored answer, to the evidence's own sentences, or to saying so; a failure is returned, never swallowed.
It is a structured comparison, not a proof: see LIMITS.
"""
from __future__ import annotations

import re

try:
    from . import evidence as ev
    from .textutil import terms
except ImportError:  # an exported package puts runtime/ on the path
    import evidence as ev
    from textutil import terms

LIMITS = ("The check compares typed facts (figures, dates, days, contacts, yes/no, a list of opposites and of commitment words) and how much of each claim's wording or meaning the evidence "
          "carries. It does not understand language: a claim that changes who or what a rule applies to without changing any of those (members for customers, indoor for outdoor), or that "
          "chains two true sentences into a false implication, can pass. It is strict on purpose: a valid rewording can be refused and the stored wording used instead.")

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30,
                "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90}
UNIT = r"(?:minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?)"
MINUTES = {"m": 1, "h": 60, "d": 1440, "w": 10080, "y": 525600}
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
CURRENCY = {"₹": "INR", "rs": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR", "$": "USD", "usd": "USD", "dollar": "USD", "dollars": "USD", "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
            "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP"}
NEGATION = re.compile(r"\b(no|not|never|cannot|can't|cant|don't|dont|doesn't|doesnt|won't|wont|isn't|aren't|unable|without|nor|none|neither)\b|\bnon-?\w+", re.I)
BOUNDS = {"MORE": r"more than|over|above|greater than|exceeding|longer than", "LESS": r"less than|under|below|fewer than|shorter than", "ATLEAST": r"at least|minimum of|minimum|no fewer than|no less than",
          "ATMOST": r"at most|maximum of|maximum|no more than|not more than|up to", "WITHIN": r"within", "BEFORE": r"before|prior to|ahead of|no later than", "AFTER": r"after|following|later than"}
OPPOSITES = [("free", "charge"), ("free", "fee"), ("free", "paid"), ("refundable", "nonrefundable"), ("open", "closed"), ("allowed", "prohibited"), ("allowed", "forbidden"), ("accept", "refuse"),
             ("accept", "decline"), ("included", "excluded"), ("required", "optional"), ("mandatory", "optional"), ("increase", "decrease"), ("available", "unavailable"), ("before", "after"),
             ("within", "after"), ("always", "never"), ("permitted", "prohibited"), ("approve", "reject"), ("early", "late")]
COMMITMENTS = ("guarantee", "guaranteed", "guarantees", "always", "never", "unlimited", "lifetime", "complimentary", "no questions asked", "100%", "full refund", "money back", "risk-free", "instantly",
               "immediately", "free", "warranty", "promise", "assured", "certainly", "definitely")
POLICY_WORDS = {ev.stem(w) for w in "refund refunded discount voucher deposit penalty insurance waive waived exempt bonus compensation replacement reimburse warranty cancel cancellation fee charge".split()}
LIMITERS = re.compile(r"\b(if|unless|provided(?: that)?|except(?: for)?|excluding|other than|subject to|only (?:if|when|for|on|with|after|before)|not valid|non-?refundable)\b", re.I)
QUANT_BOUND = re.compile(r"\b(at least|minimum(?: of)?|maximum(?: of)?|at most|no more than|no less than|up to)\b", re.I)
DEADLINE = re.compile(r"\b(within|before|after|until|by|no later than)\b", re.I)
ONLY = re.compile(r"\bonly\b", re.I)
CLOSED = re.compile(r"\b(closed|shut|off|unavailable|holiday)\b", re.I)
SAME_AS_FREE = re.compile(r"\b(?:at )?no (?:extra |additional )?(?:charge|fee|cost)s?\b|\bwithout (?:any )?charge\b|\bfree of charge\b|\bcomplimentary\b", re.I)
DAY_NAMES = re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\bweekdays?\b|\bweekends?\b|\bdaily\b", re.I)
FILLER = {ev.stem(w) for w in "sure certainly absolutely thanks thank hello hi great happy glad help please welcome question sorry apologies okay ok yes unfortunately however also note".split()}
SYNONYMS = {"free": ("no charge", "no fee", "no cost", "complimentary", "without charge", "free of charge", "at no cost")}
CITE = re.compile(r"\[(\d+)\]")
CLAUSES = re.compile(r",?\s+(?:but|while|whereas|although|though|however|so|therefore|because|which means|meaning that)\s+")
RATE = re.compile(r"(?:\bper|\beach|\bevery|\ba|\ban|/)\s?(second|minute|hour|day|week|month|year)s?\b", re.I)
MARKER_SYNONYMS = {"if": ("if", "when", "whenever", "provided", "in case", "as long as"), "unless": ("unless", "except", "if not", "only if"), "only": ("only", "just", "solely", "exclusively", "alone"),
                   "except": ("except", "excluding", "other than", "apart from", "but not", "unless"), "at least": ("at least", "minimum", "no less than", "no fewer than"),
                   "minimum": ("at least", "minimum", "no less than"), "maximum": ("at most", "maximum", "no more than", "up to"), "at most": ("at most", "maximum", "no more than", "up to"),
                   "no more than": ("no more than", "at most", "maximum", "up to"), "no less than": ("at least", "minimum", "no less than"), "up to": ("up to", "at most", "maximum", "no more than"),
                   "provided": ("provided", "if", "as long as", "when"), "excluding": ("excluding", "except", "other than", "not including"), "other than": ("other than", "except", "excluding"),
                   "subject to": ("subject to", "depending on", "depends on", "if"), "not valid": ("not valid", "invalid", "cannot be used"), "non-refundable": ("non-refundable", "nonrefundable", "not refundable")}


def _lead(text: str) -> str | None:
    """'yes' or 'no' when a text opens with it as its own word ('Yes.', 'No, ...'), after any greeting."""
    m = re.match(r"\s*(?:(?:sure|certainly|of course|okay|ok)\s*[!.,]?\s*)?(yes|no)\b\s*[.,!:;]", str(text or ""), re.I)
    return m.group(1).lower() if m else None


def same_words(text: str) -> str:
    """'no charge', 'without charge', 'complimentary' and 'free of charge' say one thing: written as 'free' on both sides of a comparison so that a negation inside them is not read as one."""
    return SAME_AS_FREE.sub(" free ", str(text or ""))


def _prep(text: str) -> str:
    """The text with citations removed and number words / 'an hour' written as figures, so that 'seven business days' and '7 business days' are one fact."""
    t = CITE.sub(" ", str(text or ""))
    t = re.sub(r"\bhalf an hour\b", "30 minutes", t, flags=re.I)
    t = re.sub(r"\b(?:an|one) hour\b", "1 hour", t, flags=re.I)
    t = re.sub(r"\ba (day|week|month|year)\b(?! (?:or|and|to))", r"1 \1", t, flags=re.I)
    words = "|".join(NUMBER_WORDS)
    after = rf"(?=[\s-]+(?:business |working |calendar |additional |extra )?(?:{UNIT}|rupees?|dollars?|euros?|pounds?|percent|times|items?|dogs?|pets?|people|guests?|persons?|appointments?)\b)"
    return re.sub(rf"\b({words})\b{after}", lambda m: str(NUMBER_WORDS[m.group(1).lower()]), t, flags=re.I)


def _minutes(value: float, unit: str) -> float:
    return value * MINUTES[unit.lower()[0]] if not unit.lower().startswith("mo") else value * 43200


def facts(text: str) -> dict:
    """The typed facts a text states."""
    t = _prep(text)
    out = {"money": set(), "clock": set(), "duration": set(), "percent": set(), "dates": set(), "days": set(), "plain": set(), "urls": set(), "emails": set(), "phones": set()}

    def take(pattern: str, handler, flags=re.I) -> None:
        nonlocal t
        for m in re.finditer(pattern, t, flags):
            handler(m)
        t = re.sub(pattern, " ", t, flags=flags)

    take(r"https?://[^\s)>\]]+|\bwww\.[^\s)>\]]+|\b[a-z0-9-]+\.(?:com|org|net|in|io|co|app|dev|uk|us)(?:/[^\s)>\]]*)?\b", lambda m: out["urls"].add(m.group(0).lower().rstrip(".,;")))
    take(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", lambda m: out["emails"].add(m.group(0).lower()))
    take(r"\+?\d[\d\s().-]{7,}\d", lambda m: out["phones"].add(re.sub(r"\D", "", m.group(0))) if len(re.sub(r"\D", "", m.group(0))) >= 8 else None)

    month = "|".join(MONTHS)
    take(rf"\b({month})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month})[a-z]*\b",
         lambda m: out["dates"].add((MONTHS[(m.group(1) or m.group(4)).lower()[:3]], int(m.group(2) or m.group(3)))))
    take(r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda m: out["dates"].add((int(m.group(2)), int(m.group(3)))))

    def clock(m):
        if m.group("ap"):
            out["clock"].add(int(m.group("h")) % 12 * 60 + int(m.group("mi") or 0) + (720 if m.group("ap").lower()[0] == "p" else 0))
        else:
            out["clock"].add(int(m.group("h2")) * 60 + int(m.group("mi2")))
    take(r"\b(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s?(?P<ap>[ap]\.?m)(?![a-z])|\b(?P<h2>[01]?\d|2[0-3]):(?P<mi2>[0-5]\d)\b", clock)

    def money(m):
        cur = CURRENCY[(m.group("c1") or m.group("c2")).lower()]
        out["money"].add((cur, float((m.group("n1") or m.group("n2")).replace(",", ""))))
    take(r"(?P<c1>₹|\brs\.?|\binr|\$|€|£|\busd|\beur|\bgbp)\s?(?P<n1>\d[\d,]*(?:\.\d+)?)|(?P<n2>\d[\d,]*(?:\.\d+)?)\s?(?P<c2>rupees?|dollars?|euros?|pounds?|inr|usd|eur|gbp)\b", money)
    take(r"(\d+(?:\.\d+)?)\s?(?:%|percent\b)", lambda m: out["percent"].add(float(m.group(1))))
    take(rf"(\d+(?:\.\d+)?)[\s-]*(business |working |calendar )?({UNIT})\b", lambda m: out["duration"].add((_minutes(float(m.group(1)), m.group(3)), bool(m.group(2) and m.group(2).strip() != "calendar"))))

    names = "|".join(DAYS)
    out["days_neg"] = set()
    for seg in re.split(r"[.!?;,]|\s+(?:and|but)\s+(?=(?:is |are |it is |we are |we're |not |no |closed|shut))", t):
        days = set()
        for x, y in re.findall(rf"\b({names})[a-z]*\s*(?:to|through|thru|till|until|-|–)\s*({names})[a-z]*\b", seg, re.I):
            i, j = DAYS[x.lower()[:3]], DAYS[y.lower()[:3]]
            days |= {(i + k) % 7 for k in range(((j - i) % 7) + 1)}
        days |= {DAYS[d.lower()[:3]] for d in re.findall(rf"\b({names})[a-z]*\b", seg, re.I)}
        if re.search(r"\bweekdays?\b", seg, re.I):
            days |= {0, 1, 2, 3, 4}
        if re.search(r"\bweekends?\b", seg, re.I):
            days |= {5, 6}
        if re.search(r"\b(daily|every day|seven days a week|7 days a week)\b", seg, re.I):
            days |= set(range(7))
        out["days_neg" if (NEGATION.search(seg) or CLOSED.search(seg)) else "days"] |= days
    t = re.sub(r"\b\d{1,3}(?:,\d{3})+\b", lambda m: m.group(0).replace(",", ""), t)
    out["plain"] = {float(x) for x in re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?:s|st|nd|rd|th)?(?![\w])", t)}
    return out


def bounds(text: str) -> set[str]:
    return {k for k, p in BOUNDS.items() if re.search(rf"\b(?:{p})\b", text, re.I)}


def _has(word: str, text: str) -> bool:
    low = text.lower()
    if any(alt in low for alt in SYNONYMS.get(word, ())):
        return True
    return word in low if (" " in word or "%" in word) else ev.present(ev.stem(word), set(terms(text)) | set(re.findall(r"[a-z%-]+", low)))


def _closest(claim: str, windows: list[dict], sim) -> tuple[list[dict], list[dict], float, float]:
    """The evidence windows that carry a claim: (up to three that each share a good part of its words or are close in meaning, the smallest set of them that carries it, the share of
    the claim's words found in the first together, the best meaning-closeness). The first are compared for facts; only the smallest set puts its conditions on the answer."""
    ct = list(dict.fromkeys(content(claim)))
    sims = sim(claim, [w["text"] for w in windows]) if (sim is not None and windows) else [0.0] * len(windows)
    scored = []
    for w, s in zip(windows, sims):
        have = set(content(w["text"]))
        scored.append(((sum(ev.present(t, have) for t in ct) / len(ct)) if ct else 1.0, s, w, have))
    scored.sort(key=lambda x: (-(x[0] + 0.5 * x[1]), len(x[2]["text"])))
    chosen = [x for x in scored if x[0] >= 0.3 or x[1] >= 0.5][:3]
    if not chosen:
        return [], [], 0.0, max((x[1] for x in scored), default=0.0)
    union = set().union(*(x[3] for x in chosen))
    covered = lambda have: (sum(ev.present(t, have) for t in ct) / len(ct)) if ct else 1.0  # noqa: E731
    core, have = [chosen[0]], set(chosen[0][3])
    for x in chosen[1:]:  # another window joins as soon as it carries a word of the claim that the others do not: its limits then bind the answer too
        if covered(have | x[3]) > covered(have):
            core.append(x)
            have |= x[3]
    return [x[2] for x in chosen], [x[2] for x in core], covered(union), max(x[1] for x in chosen)


def _words(sentence: str, start: int, count: int) -> str:
    return " ".join(re.split(r"[,;.]", sentence[start:])[0].split()[:count])


def _marker(clause: str) -> str | None:
    """The limiting word a clause begins with or contains ('only', 'unless', 'at least' ...), if any."""
    low = clause.lower()
    for key in sorted(MARKER_SYNONYMS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(key)}\b", low):
            return key
    return None


def _condition_clauses(sentence: str) -> list[str]:
    """The limits a sentence puts on its statement: an 'if' / 'unless' / 'except' clause, 'only', 'at least 6 hours', and a deadline ('within 7 days', only when it has a figure)."""
    out = [_words(sentence, m.start(), 10) for m in LIMITERS.finditer(sentence)]
    out += [_words(sentence, m.start(), 5) for m in QUANT_BOUND.finditer(sentence)]
    out += [" ".join(sentence[:m.end()].split()[-3:]) for m in ONLY.finditer(sentence) if not LIMITERS.match(sentence[m.start():])]
    for m in DEADLINE.finditer(sentence):
        clause = _words(sentence, m.start(), 5)
        f = facts(clause)
        if f["duration"] or f["clock"] or f["dates"] or f["days"]:
            out.append(clause)
    return [c for c in dict.fromkeys(out) if terms(c)]


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", _prep(text).replace(",", "")))


def _kept(clause: str, answer_text: str, answer_terms: set[str]) -> bool:
    """Does the answer (or the customer's own question) still carry a limit: every figure of it, and most of its words?"""
    if _numbers(clause) - _numbers(answer_text):
        return False
    marker = _marker(clause)
    if marker and not any(re.search(rf"\b{re.escape(alt)}\b", answer_text, re.I) for alt in MARKER_SYNONYMS[marker]):
        return False  # the limit's own word is gone ("only", "unless", "at least"): whatever else remains, the limit does not
    ct = list(dict.fromkeys(t for t in content(clause) if not t.isdigit()))
    return (sum(ev.present(t, answer_terms) for t in ct) / len(ct) >= 0.6) if ct else True


def content(text: str) -> list[str]:
    """The words that carry meaning in a text: weekday names are left out, because the typed day comparison judges them (a range 'Tuesday to Sunday' includes Wednesday)."""
    return terms(DAY_NAMES.sub(" ", text))


def verify(answer: str, sources: list[dict], mode: str = "grounded", question: str = "", sim=None) -> dict:
    """Is `answer` inside the evidence in `sources` ([{ref, text}, ...])? mode 'grounded': a combination of passages; 'polish': a rewording of one stored answer, which must also keep every
    figure of it. `question` (the customer's own words) may restate a condition; `sim(claim, texts)` (optional) measures closeness in meaning. Returns
    {passed, issues:[{type, claim, detail, ref}], claims:[{text, status, refs, issues}], mode, limits}."""
    issues: list[dict] = []
    claims = []
    text = str(answer or "").strip()
    if not text:
        return {"passed": False, "issues": [{"type": "empty", "claim": "", "detail": "there is no answer to check", "ref": None}], "claims": [], "mode": mode, "limits": LIMITS}
    refs = {int(s["ref"]) for s in sources}
    for n in {int(x) for x in CITE.findall(text)} - refs:
        issues.append({"type": "bad_reference", "claim": f"[{n}]", "detail": f"there is no source [{n}]", "ref": n})
    lead_claim, lead_evidence = _lead(text), {_lead(s["text"]) for s in sources} - {None}
    if lead_claim and lead_evidence and lead_claim not in lead_evidence:  # an answer that opens with "No." where the evidence opens with "Yes." (or the reverse)
        issues.append({"type": "contradiction", "claim": text[:160], "detail": f"the answer opens with '{lead_claim}' where the evidence opens with '{sorted(lead_evidence)[0]}'", "ref": sources[0]["ref"] if sources else None})
    windows = [{"text": w, "ref": s["ref"]} for s in sources for w in ev.windows(same_words(s["text"]))]
    evidence_facts = facts(" ".join(same_words(s["text"]) for s in sources))
    ask = facts(question)
    answer_facts, answer_terms = facts(text), set(content(same_words(text))) | set(content(question))
    answer_and_question = same_words(text) + " " + question
    used: list[dict] = []
    for shown in [c for sentence in ev.sentences(CITE.sub(" ", text)) for c in CLAUSES.split(sentence)]:
        found: list[dict] = []
        claim = same_words(shown)
        ct = content(claim)
        cf = facts(claim)
        if (not ct or all(ev.stem(t) in FILLER for t in ct)) and not any(cf[k] for k in cf):
            claims.append({"text": shown, "status": "no_factual_content", "refs": [], "issues": []})
            continue
        near, core, cover, close = _closest(claim, windows, sim)
        near_text = " ".join(w["text"] for w in near)
        nf = facts(near_text)
        allowed = {k: nf[k] | evidence_facts[k] if k in ("urls", "emails", "phones") else nf[k] | ask[k] for k in nf}  # a contact must be in the evidence at all; a figure in what carries this claim or in the question

        def flag(kind: str, detail: str) -> None:
            found.append({"type": kind, "claim": shown, "detail": detail, "ref": near[0]["ref"] if near else None})

        supported = bool(near) and (cover >= 0.8 or (close >= 0.65 and cover >= 0.4))
        if not supported:
            flag("unsupported_claim", f"only {int(100 * cover)}% of its wording is in the evidence" + ("" if sim is None else f", closest meaning {close:.2f}"))
        for key, label in (("money", "price"), ("clock", "time"), ("duration", "duration"), ("percent", "percentage"), ("dates", "date"), ("days", "weekday")):
            extra = cf[key] - allowed[key]
            if extra:
                kind = "changed_number" if (nf[key] or evidence_facts[key]) else "added_detail"
                flag(kind, f"{label} {sorted(extra, key=str)} is not what the evidence says ({sorted(nf[key] or evidence_facts[key], key=str) or 'none'})")
        negated_days = cf["days_neg"] - (nf["days_neg"] | ({0, 1, 2, 3, 4, 5, 6} - nf["days"] if nf["days"] else set()))
        if negated_days:
            flag("changed_number", f"the claim says it is closed or unavailable on {sorted(negated_days)}, which the evidence does not say")
        extra_plain = cf["plain"] - allowed["plain"]
        if extra_plain:
            flag("changed_number" if nf["plain"] else "added_detail", f"number {sorted(extra_plain)} is not in the evidence")
        for key, label in (("urls", "web address"), ("emails", "e-mail address"), ("phones", "phone number")):
            if cf[key] - allowed[key]:
                flag("added_detail", f"{label} {sorted(cf[key] - allowed[key])[0]} is not in the evidence")
        if near:
            neg_claim = bool(NEGATION.search(claim))
            mine = set(content(claim))
            sents = list(dict.fromkeys(c for w in near for x in ev.sentences(w["text"]) for c in CLAUSES.split(x)))  # polarity is read sentence by sentence: a window of two can hold a yes and a no at once
            for w in sents:
                shared = mine & set(content(w))
                if len(shared) >= min(2, len(mine)) and bool(NEGATION.search(w)) != neg_claim and not any(bool(NEGATION.search(x)) == neg_claim and shared <= set(content(x)) for x in sents):
                    flag("contradiction", "the claim says the opposite of the evidence (yes where it says no, or the reverse)")
                    break
            for a, b in OPPOSITES:
                for x, y in ((a, b), (b, a)):
                    if _has(x, claim) and not _has(x, near_text) and _has(y, near_text) and not (neg_claim != bool(NEGATION.search(near_text))):
                        flag("misrepresented_policy", f"the claim says '{x}' where the evidence says '{y}'")
            claim_rates, evidence_rates = {u.lower() for u in RATE.findall(claim)}, {u.lower() for u in RATE.findall(near_text)} | {u.lower() for u in RATE.findall(question)}
            if claim_rates and evidence_rates and not claim_rates <= evidence_rates:
                flag("changed_number", f"the claim measures per {sorted(claim_rates - evidence_rates)[0]} where the evidence says per {sorted(evidence_rates)[0]}")
            if bounds(claim) - bounds(near_text):
                flag("misrepresented_policy", f"the claim limits it as '{sorted(bounds(claim) - bounds(near_text))[0].lower()}' and the evidence does not")
            for word in COMMITMENTS:
                if _has(word, claim) and not _has(word, near_text) and not _has(word, question):
                    flag("misrepresented_policy", f"the claim promises '{word}' and the evidence does not")
                    break
            novel = [t for t in dict.fromkeys(ct) if not ev.present(t, set(terms(near_text)))]
            if any(t in POLICY_WORDS or ev.stem(t) in POLICY_WORDS for t in novel):
                flag("added_detail", f"it brings in '{next(t for t in novel if t in POLICY_WORDS or ev.stem(t) in POLICY_WORDS)}', which the evidence does not mention")
            used += core if cover >= 0.5 else []
        issues += found
        claims.append({"text": shown, "status": "supported" if not found else "rejected", "refs": sorted({w["ref"] for w in near}), "issues": [i["type"] for i in found]})
    for w in {id(x): x for x in (used if mode == "grounded" else windows)}.values():  # a limit of a sentence the answer relies on must survive
        for clause in _condition_clauses(w["text"]):
            if not _kept(clause, answer_and_question, answer_terms):
                issues.append({"type": "missing_condition", "claim": text[:160], "detail": f"the condition '{clause[:120]}' is left out", "ref": w["ref"]})
    if mode == "polish":  # a rewording may not lose a figure
        for key, label in (("money", "price"), ("clock", "time"), ("duration", "duration"), ("percent", "percentage"), ("dates", "date"), ("days", "weekday"), ("plain", "number")):
            lost = evidence_facts[key] - answer_facts[key]
            if lost:
                issues.append({"type": "dropped_detail", "claim": text[:160], "detail": f"the {label} {sorted(lost, key=str)} of the stored answer is missing", "ref": sources[0]["ref"] if sources else None})
    unique, keys = [], set()
    for i in issues:
        k = (i["type"], i["detail"], i["claim"])
        if k not in keys:
            keys.add(k)
            unique.append(i)
    return {"passed": not unique, "issues": unique, "claims": claims, "mode": mode, "limits": LIMITS}
