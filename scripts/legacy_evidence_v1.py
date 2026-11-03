# FROZEN COPY of runtime/evidence.py as it was before the "improve evidence sufficiency" milestone. Used only to reproduce the "before" numbers; do not edit.
"""Evidence-aware answering: a FAQ answer is returned as the answer only when it actually supports what was asked.

Similarity says two questions are alike. It does not say the stored answer contains the requested fact ("How much is a bath?" is very similar to
"What services do you offer?", whose answer lists a bath and no price), so similarity is kept apart from three other signals:

    retrieval_similarity   how alike the best FAQ question is (cosine, NOT a probability, never a confidence)
    answer_sufficiency     the share of the question's parts that the FAQ answer explicitly supports
    evidence_support       the share of parts supported by the evidence finally used (FAQ answer or document passages)
    response_confidence    a category (high / medium / low / none) from the rules below; it is not calibrated and says so

Outcomes:  faq (the stored answer is sufficient)  ·  grounded (an answer assembled only from document passages, with references)  ·
           insufficient (nothing available answers it: say so, offer clarification or escalation)  ·  conflict (sources disagree: say so, do not pick).

Standard library only, no model: the checks are explicit and inspectable (what each part of the question asks, which words and which kind of detail it
needs, which sentence supplies them), which also makes their limits plain: they judge wording, not meaning. See LIMITS.
"""
from __future__ import annotations

import re

LIMITS = ("Sufficiency is judged from the words and the kind of detail a question asks for (a price, a time, a duration, a place, a contact, a count) "
          "and which sentence supplies them. It can miss a paraphrase that shares no words, and can be satisfied by a sentence that mentions the right words "
          "but says something else. Conflicts are found for numbers with units and for yes/no statements about the same thing, not for every kind of disagreement.")

STOP = set("""a an the of in on at to for from by with about as into over under and or but if then than so is are was were be been being am do does did done can could will would
should may might must shall have has had i you he she it we they me my your our their his her its this that these those there here what which who whom whose when where why how
please tell know want need like get got let also just any some more most very much many yes no not happen happens happened time times kind type thing""".split())
ASKS = {  # what kind of detail a part of the question wants, and how that detail looks when a text supplies it
    "price": (r"how much|\bcost|\bprice|\bfees?\b|\bcharges?\b|\brates?\b|\bpriced?\b",
              r"(₹|\brs\.?|\$|€|£|\binr\b)\s?\d|\d+\s?(rupees?|dollars?|inr|usd|euros?)\b|\bfree\b|\bno (charge|fee|cost)\b|\bcomplimentary\b"),
    "duration": (r"how long|how many (minutes|hours|days|weeks)|\bduration\b|how soon|how quickly",
                 r"\d+\s?(to|-|–)?\s?\d*\s?(min|mins|minutes?|hours?|hrs?|days?|weeks?)\b|\b(one|two|three|four|five|six|half) (hour|day|week|minute)s?\b|\ban hour\b"),
    "time": (r"what time|\bwhen\b|opening hours|\bhours\b|closing|\bclose\b|which days|what days|\bschedule\b|\bopen\b",
             r"\b\d{1,2}(:\d{2})?\s?(am|pm)\b|\b(mon|tues?|wednes|thurs?|fri|satur|sun)(day)?s?\b|\bclosed\b|\b24 ?(hours|hrs|/7)\b|\b\d{1,2}:\d{2}\b"),
    "place": (r"\bwhere\b|\baddress\b|\blocation\b|\blocated\b|which (area|part|city|neighbourhood)|neighbourhood",
              r"\b(located|address|street|road|nagar|lane|floor|building|area|city|near)\b|\bin [A-Z][a-z]+"),
    "contact": (r"phone number|contact number|\bemail\b|how (can|do|should) i (contact|reach|call)|\bwhatsapp\b|\bcall (you|us)\b",
                r"\d{5,}|@|whatsapp"),
    "count": (r"how many\b(?! (minutes|hours|days|weeks))", r"\d+|\b(one|two|three|four|five|six|seven|eight|nine|ten|none)\b"),
}
ASK_RE = {k: (re.compile(q, re.I), re.compile(a, 0 if k == "place" else re.I)) for k, (q, a) in ASKS.items()}
NEGATIVE = re.compile(r"\b(no|not|never|cannot|can't|cant|don't|dont|doesn't|doesnt|won't|unable|only)\b", re.I)
QUANTITY = re.compile(r"(?P<money>(?:₹|rs\.?|\$|€|£)\s?(?P<m>\d[\d,]*))|(?P<time>\b(?P<h>\d{1,2})(?::(?P<mi>\d{2}))?\s?(?P<ap>am|pm)\b)|(?P<dur>\b(?P<d>\d+)\s?(?P<u>min|mins|minutes?|hours?|hrs?|days?)\b)", re.I)
MIN_SUPPORT = 0.75  # a part is supported when this share of the words it asks about appear in the sentence that answers it ("explicitly": one missing word of two fails)
QUESTION_WORDS = r"(?:what|how|when|where|who|which|do|does|did|can|could|is|are|will|would|should|may)"


SUFFIXES = ("ations", "ation", "ments", "ment", "ings", "ing", "ed", "es", "s")


def stem(word: str) -> str:
    w = word.lower()
    for suffix in SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiou":
        w = w[:-1]
    return w


STOP_STEMS = {stem(w) for w in STOP}


def terms(text: str) -> list[str]:
    return [t for t in (stem(w) for w in re.findall(r"[^\W_]+", text.lower()) if w not in STOP and (len(w) > 1 or w.isdigit())) if t not in STOP_STEMS]


def split_parts(question: str) -> list[str]:
    """The separate things a question asks: "Do you groom cats and what are your hours?" is two."""
    q = re.sub(r"\s+", " ", question.strip())
    out: list[str] = []
    for piece in (p.strip(" ,") for p in re.split(r"\?+|;", q)):
        for sub in re.split(rf"\s*(?:,|\band\b|&|\balso\b)\s+(?={QUESTION_WORDS}\b)", piece, flags=re.I):
            if terms(sub):
                out.append(sub.strip(" ,"))
    return out or [q]


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def asks_for(part: str) -> list[str]:
    return [k for k, (q, _) in ASK_RE.items() if q.search(part)]


def support(part: str, text: str, topic: set[str] | None = None) -> tuple[float, bool]:
    """(share of the part's words found in `text`, whether the kind of detail it asks for is there). `topic` words (the subject the FAQ question
    already names) do not have to be repeated by the answer."""
    need = [t for t in dict.fromkeys(terms(part)) if not (topic and t in topic) and not t.isdigit()]  # numbers describe the asker's situation ("20 minutes late"), not what is asked
    have = set(terms(text))
    cover = (sum(t in have for t in need) / len(need)) if need else 1.0
    kinds = asks_for(part)
    return cover, all(ASK_RE[k][1].search(text) for k in kinds)


SEM_MIN = 0.45  # how alike (cosine) a question part and ONE answer sentence must be for that sentence to count as supporting it when the words differ
SEM_COVER = 0.34  # ... and at least this share of the part's words must still be in it. Both are only used when a model supplies `sim`.


def _best_sentence(part: str, text: str, topic: set[str] | None = None, sim=None, sem_min: float = SEM_MIN) -> tuple[float, bool, str]:
    """The one sentence, or two neighbouring ones, of `text` that best supports the part: (share of its words found, whether the kind of detail
    it asks for is there, the text). Two sentences are tried because an answer often says "open Tuesday to Sunday." then "Closed on Mondays." """
    sents = sentences(text) or [text]
    windows = sents + [f"{x} {y}" for x, y in zip(sents, sents[1:])]
    sems = sim(part, windows) if sim is not None else [0.0] * len(windows)  # how alike the question part and each window of the answer are, by meaning
    best, best_key = (-1.0, False, ""), None
    for w, sem in zip(windows, sems):
        cover, kind_ok = support(part, w, topic)
        if sem >= sem_min and cover >= SEM_COVER:
            cover = max(cover, MIN_SUPPORT)  # the words differ but the sentence addresses the question: counted as supported (the detail kind must still be present)
        key = (kind_ok, cover)
        if best_key is None or key > best_key:  # a window with the right kind of detail beats one without; then more of the words; the shorter wins a tie
            best, best_key = (cover, kind_ok, w), key
    return best


def supported(cover: float, kind_ok: bool, topic_only: bool = False) -> bool:
    return kind_ok and (cover >= MIN_SUPPORT or topic_only)


def assess_faq(question: str, hit: dict, sim=None, sem_min: float = SEM_MIN) -> dict:
    """Does this FAQ answer explicitly support every part of the question? Independent of how similar the question is: a similarity of 1.00 changes nothing here."""
    topic = set(terms(hit.get("question", ""))) | (set(terms(hit.get("matched", ""))) if hit.get("matched_kind", "q") == "q" else set())
    parts = []
    for part in split_parts(question):
        cover, kind_ok, sentence = _best_sentence(part, hit.get("answer", ""), topic, sim, sem_min)
        need = [t for t in terms(part) if t not in topic and not t.isdigit()]
        parts.append({"part": part, "supported": bool(hit.get("answer")) and supported(cover, kind_ok, topic_only=not need and not asks_for(part)),
                      "coverage": round(cover, 2), "detail_present": kind_ok, "asks_for": asks_for(part), "sentence": sentence})
    return {"parts": parts, "sufficiency": sum(p["supported"] for p in parts) / len(parts), "sufficient": all(p["supported"] for p in parts)}


def _quantities(text: str) -> dict[str, set[float]]:
    """Numbers with units in a sentence, by kind: money, clock times, durations (in minutes)."""
    out: dict[str, set[float]] = {}
    for m in QUANTITY.finditer(text):
        if m.group("money"):
            out.setdefault("money", set()).add(float(m.group("m").replace(",", "")))
        elif m.group("time"):
            h = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
            out.setdefault("clock", set()).add(h * 60 + int(m.group("mi") or 0))
        else:
            unit = m.group("u").lower()
            out.setdefault("duration", set()).add(float(m.group("d")) * (60 if unit.startswith(("h")) else 1440 if unit.startswith("d") else 1))
    return out


def conflict_between(part: str, a: str, b: str) -> str | None:
    """A reason when two sentences about the same thing disagree: different amounts of the same kind, or one says no where the other does not."""
    qa, qb = _quantities(a), _quantities(b)
    for kind in qa.keys() & qb.keys():
        if qa[kind] and qb[kind] and not (qa[kind] & qb[kind]):
            return f"one says {sorted(qa[kind])} and the other {sorted(qb[kind])} ({kind})"
    if asks_for(part) == [] and part.split()[0].lower() in ("do", "does", "can", "is", "are", "will", "may", "could", "would"):
        shared = set(terms(a)) & set(terms(b)) & set(terms(part))
        if len(shared) >= 2 and bool(NEGATIVE.search(a)) != bool(NEGATIVE.search(b)) and not re.search(r"\bonly\b", a + b, re.I):
            return "one says yes where the other says no"
    return None


def locate(hit: dict) -> str:
    """Where a passage is: its file or address, its section, and which piece of it."""
    ordinal = re.search(r"-(\d+)$", hit.get("entry_id", hit.get("id", "")))
    return f"{hit.get('source') or 'document'} › {hit.get('question', '')}" + (f" (passage {int(ordinal.group(1))})" if ordinal else "")


def respond(question: str, faq_hits: list[dict], passage_hits: list[dict], llm=None, similarity_floor: float = 0.0, sim=None, sem_min: float = SEM_MIN) -> dict:
    """Decide what to say. `faq_hits` and `passage_hits` are the best entries of each kind, best first, as the engines return them
    ({entry_id, question, answer, score, matched, matched_kind, source, kind}). `sim(part, [texts]) -> [cosines]` is optional (an embedding model's view of how well a sentence addresses a question part); `llm(prompt) -> str` is optional and only ever asked to
    combine passages that are already known to support the answer; without it the answer is assembled from the passages' own sentences."""
    parts = split_parts(question)
    top = faq_hits[0] if faq_hits else None
    top_sim = float(top["score"]) if top else 0.0
    assessed = [(h, assess_faq(question, h, sim, sem_min)) for h in faq_hits[:3]]
    ranked = sorted(assessed, key=lambda ha: (ha[1]["sufficient"], ha[1]["sufficiency"], ha[0]["score"]), reverse=True)
    best_faq, a = ranked[0] if ranked else (None, {"sufficiency": 0.0, "sufficient": False, "parts": [{"part": p, "supported": False} for p in parts]})
    base = {"question": question, "parts": [p["part"] for p in a["parts"]], "retrieval_similarity": top_sim, "retrieval_similarity_note": "cosine similarity of the best FAQ question: not a probability",
            "answer_sufficiency": a["sufficiency"], "faq_considered": best_faq["entry_id"] if best_faq else None, "limits": LIMITS}

    # passages that bear on each part (used for stage two, and to check a sufficient FAQ answer against the documents)
    evidence: list[list[dict]] = []
    for part in parts:
        found = []
        for h in passage_hits:
            cover, kind_ok, sent = _best_sentence(part, h.get("answer", ""), set(terms(h.get("question", ""))), sim, sem_min)
            if supported(cover, kind_ok):
                found.append({"hit": h, "sentence": sent, "coverage": cover})
        evidence.append(sorted(found, key=lambda e: (e["coverage"], e["hit"]["score"]), reverse=True))

    def conflict(part: str, ours: str, theirs: list[dict], who: str) -> dict | None:
        for e in theirs:
            why = conflict_between(part, ours, e["sentence"])
            if why:
                return {"part": part, "between": [who, locate(e["hit"])], "reason": why, "texts": [ours, e["sentence"]]}
        return None

    def done(outcome: str, **kw) -> dict:
        return {**base, "outcome": outcome, **kw}

    ask = "Could you tell me which of these you mean, or would you like me to pass your question to a person?"
    if best_faq is not None and a["sufficient"]:  # A: the stored answer supports every part
        for part, ev in zip(a["parts"], evidence):
            c = conflict(part["part"], part["sentence"] or best_faq["answer"], ev, f"FAQ {best_faq['entry_id']}")
            if c:  # D: a document says something different about the same thing
                return done("conflict", answer=None, conflicts=[c], evidence_support=0.0, response_confidence="none", escalate=True,
                            message=f"The FAQ and the documents disagree about this ({c['reason']}). I will not choose between them. {ask}", used_llm=False)
        return done("faq", answer=best_faq["answer"], sources=[{"ref": 1, "kind": "faq", "id": best_faq["entry_id"], "source": best_faq.get("source") or "FAQ", "location": best_faq["question"]}],
                    evidence_support=1.0, response_confidence="high" if top_sim >= similarity_floor else "medium", escalate=False, used_llm=False,
                    confidence_note="a category from the rules, not a calibrated probability")

    # B / C / D: the FAQ does not (fully) answer: look in the documents, and use the parts of FAQ answers that do support something
    chosen, unanswered, refs = [], [], []
    for i, part in enumerate(parts):
        options = evidence[i][:2]
        if not options and best_faq is not None and i < len(a["parts"]) and a["parts"][i]["supported"]:
            options = [{"hit": best_faq, "sentence": a["parts"][i]["sentence"], "coverage": a["parts"][i]["coverage"]}]
        if not options:
            unanswered.append(part)
            continue
        chosen.append((part, options))
    for part, options in chosen:  # D: sources that disagree about the same thing
        lead = options[0]
        others = [o for o in options[1:] if o["hit"].get("source") != lead["hit"].get("source")]
        if best_faq is not None and lead["hit"] is not best_faq and best_faq.get("answer"):
            cover, kind_ok, sent = _best_sentence(part, best_faq["answer"], set(terms(best_faq["question"])), sim, sem_min)
            if sent and supported(cover, kind_ok):
                others.append({"hit": best_faq, "sentence": sent})
        c = conflict(part, lead["sentence"], others, locate(lead["hit"]))
        if c:
            return done("conflict", answer=None, conflicts=[c], evidence_support=0.0, response_confidence="none", escalate=True,
                        message=f"The sources disagree about this ({c['reason']}). I will not choose between them. {ask}", used_llm=False)
    support_share = len(chosen) / len(parts)
    if unanswered:  # C: nothing (or only part of it) is covered
        known = []
        for part, options in chosen:
            known.append({"part": part, "text": options[0]["sentence"], "reference": locate(options[0]["hit"])})
        what = "; ".join(unanswered)
        return done("insufficient", answer=None, partial=known, unanswered_parts=unanswered, evidence_support=support_share, response_confidence="none" if not known else "low", escalate=True,
                    message=(("I can answer only part of this. " if known else "The information I have does not answer this. ") + f"Nothing available covers: {what}. " + ask), used_llm=False)

    sources, lines, used = [], [], {}
    for part, options in chosen:
        opt = options[0]
        hit = opt["hit"]
        key = hit.get("entry_id")
        if key not in used:
            used[key] = len(sources) + 1
            kind = hit.get("kind", "passage")
            sources.append({"ref": used[key], "kind": kind, "id": key, "source": hit.get("source") or ("FAQ" if kind == "faq" else "document"),
                            "location": locate(hit) if kind != "faq" else hit.get("question", ""), "text": opt["sentence"], "similarity": float(hit["score"])})
        elif opt["sentence"] not in sources[used[key] - 1]["text"]:
            sources[used[key] - 1]["text"] += " " + opt["sentence"]
        lines.append((opt["sentence"], used[key]))
    extractive = " ".join(f"{s} [{r}]" for s, r in dict.fromkeys(lines))
    needs_synthesis = len({r for _, r in lines}) > 1
    answer, used_llm = extractive, False
    if needs_synthesis and llm is not None:  # the only place a model is asked anything: to combine passages already known to support the answer
        text = _synthesize(question, sources, llm)
        if text:
            answer, used_llm = text, True
    return done("grounded", answer=answer, sources=sources, evidence_support=1.0, response_confidence="medium" if len(sources) > 1 else "high" if top_sim >= similarity_floor else "medium",
                escalate=False, used_llm=used_llm, confidence_note="a category from the rules, not a calibrated probability",
                note=None if used_llm or not needs_synthesis else "assembled from the passages' own sentences; no model was used")


def _synthesize(question: str, sources: list[dict], llm) -> str | None:
    """Ask a model to combine passages, then refuse its answer unless it stays inside them: every number it states must be in the passages, and nearly every
    content word must be too. Anything else (an invented fact, a general-knowledge addition) is discarded and the assembled passages are used instead."""
    body = "\n".join(f"[{s['ref']}] {s['text']}" for s in sources)
    prompt = ("Answer the question using ONLY the numbered passages. If they do not contain the answer, reply exactly: INSUFFICIENT. Do not add facts, policies, "
              f"prices or times that are not in the passages. Cite passage numbers like [1].\n\nPassages:\n{body}\n\nQuestion: {question}\nAnswer:")
    try:
        out = (llm(prompt) or "").strip()
    except Exception:  # noqa: BLE001 - a failing model must never break answering
        return None
    if not out or out.upper().startswith("INSUFFICIENT"):
        return None
    allowed = set(terms(body)) | set(terms(question))
    numbers = set(re.findall(r"\d+(?:[.,:]\d+)?", out))
    if numbers - set(re.findall(r"\d+(?:[.,:]\d+)?", body + " " + question)):
        return None
    words = [t for t in terms(re.sub(r"\[\d+\]", " ", out)) if not t.isdigit()]
    if words and sum(w in allowed for w in words) / len(words) < 0.85:
        return None
    return out
