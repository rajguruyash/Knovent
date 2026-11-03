"""THE RESPONSE ENGINE. Turns an assessment (evidence.assess) into what is said: the stored FAQ answer, an answer assembled from the evidence, an answer to part of the question with the
rest named as unknown, a report that sources disagree, a request for clarification, or an honest "this does not answer it". It never decides whether something is supported (that is
decided before it is called) and it never adds a fact: every sentence it returns is a sentence of a source, with a reference, or a model's rewording that verify.py accepted.

Outcomes: faq · grounded · partial · insufficient · conflict · clarification (see evidence.py for the first five).

An optional model (generate.py) may reword a stored answer that is already sufficient (polish) or combine passages that already support the answer (grounded). Its text is used only if
verify.py passes it. Otherwise the evidence's own wording is used, and the result says that a model was tried, what the check found and what was done instead: a failed check is never
silent. The verification report, the model's part and any fallback are always in the result.
"""
from __future__ import annotations

import re

try:
    from . import evidence as ev
    from . import generate
except ImportError:
    import evidence as ev
    import generate

ASK = "Could you tell me which of these you mean, or would you like me to pass your question to a person?"
REFERENTIAL = re.compile(r"^\s*(and\s+)?(what|how)\s+about\s+(it|that|this|them|those)\b|^\s*(and\s+)?(it|that|this|them|those)\s*\??\s*$", re.I)


OPEN_ASK = re.compile(r"\bwhat happens\b|\bwhat is the process\b|\bwhat are the steps\b|\bhow does\b.{0,60}\bwork\b|\bexplain\b|\bdescribe\b|\btell me about\b", re.I)


ATTACHES = re.compile(r"^(however|but|unless|except|exceptions?|note|this|that|these|those|it|they|also|in addition|otherwise|if|once|when|after|before)\b", re.I)  # a sentence that belongs to the one before it
CONDITIONAL = re.compile(r"\b(unless|except|exception|excluding|only if|only when|provided|subject to|not available|does not apply|do not apply|not refunded|not eligible|not covered|cannot|must|requires?|at least|no more than|up to)\b", re.I)
BROAD = re.compile(r"\b(polic(y|ies)|process|procedure|rules?|terms|conditions?|requirements?|steps|explain|describe|tell me about|how (does|do)\b.{0,60}\bwork|what happens)\b", re.I)
SPAN_LIMIT = 4  # sentences from one passage
CROSS_LIMIT = 2  # conditions from other sources


def span(a: dict, p: int, i: int, part: str) -> str:
    """What a passage says that answers a part of the question, not only the one best sentence. The best window; the sentences that attach to it ("However ...", "This does not apply
    to ..."); for a question about a policy or a process, the other sentences of the passage that are about the same thing; for a question about exceptions or conditions, the sentences
    that state one. Sentences that share nothing with the question are left out, and so is everything else around them. At most SPAN_LIMIT sentences."""
    h, g = a["pool"][i], a["grid"][p][i]
    lead = g["sentence"]
    if h.get("kind", "faq") == "faq":
        return lead
    sents = ev.sentences(h.get("answer", ""))
    mine = ev.sentences(lead)
    idx = sorted(sents.index(x) for x in mine if x in sents)
    if not idx:
        return lead
    keep = set(range(idx[0], idx[-1] + 1))
    kt = [t for t in dict.fromkeys(ev.terms(part)) if t not in ev.GENERIC and t not in ev.FILLER and t not in ev.ASK_WORDS and not t.isdigit()]
    kind_asked = bool(ev.asks_for(part))
    claims = [k for k, (q, _) in ev.CLAIM_RE.items() if q.search(part)]
    broad = bool(BROAD.search(part)) or (not kind_asked and not claims and 0 < len(kt) <= 2 and re.match(r"\s*(what|how|tell|explain|describe)\b", part, re.I) is not None)
    touches = lambda t: any(ev.present(x, set(ev.terms(t))) for x in kt)  # noqa: E731
    for k, t in enumerate(sents):
        if k in keep:
            continue
        adjoins = (k - 1 in keep) and ATTACHES.match(t)
        states_claim = any(ev.CLAIM_RE[c][1].search(t) for c in claims if c in ("exception", "condition")) and touches(t)
        about_it = broad and touches(t)
        follows = OPEN_ASK.search(part) and not kind_asked and k in (idx[-1] + 1, idx[-1] + 2)
        if adjoins or states_claim or about_it or follows:
            keep.add(k)
        if len(keep) >= SPAN_LIMIT:
            break
    return " ".join(sents[k] for k in sorted(keep))


def conditions_elsewhere(a: dict, p: int, i: int, part: str) -> list[tuple[str, int]]:
    """Conditions and exceptions that another source states about the same kind of thing as the best one ("Prices exclude VAT" for a price in a table): the sentence, and which candidate
    it is in. It must say something conditional, and share with the question a word that names what is asked about (the thing, or the kind of figure) that is not just the subject every
    source shares."""
    own = a["pool"][i]["entry_id"]
    common = a.get("common", set())
    kt = {t for t in ev.terms(part) if t not in ev.GENERIC and t not in ev.FILLER and not t.isdigit() and t not in common}
    out = []
    for j in range(len(a["pool"])):
        h2, g2 = a["pool"][j], a["grid"][p][j]
        t = g2["sentence"]
        if h2["entry_id"] == own or not t or g2["class"] == "none" or t in a["grid"][p][i]["sentence"]:
            continue
        if CONDITIONAL.search(t) and any(ev.present(x, set(ev.terms(t))) for x in kt):
            out.append((t, j))
        if len(out) >= CROSS_LIMIT:
            break
    return out


def unspecific(question: str) -> bool:
    """A question with no content words at all ("what about that?", "?"): nothing in a knowledge base can be said to answer it."""
    return not ev.terms(question) or bool(REFERENTIAL.search(question))


def clarification(a: dict) -> dict | None:
    """Is the question too unspecific to answer, and would asking help? Only called when nothing answers it. Two cases: it names nothing at all ("what about that?"), or it is a few
    words naming no subject and two or more different entries are about equally close to it."""
    q = a["question"]
    if unspecific(q):
        return {"reason": "the question does not say what it is about", "options": []}
    subject = [t for t in ev.terms(q) if t not in ev.ASK_WORDS]
    if len(subject) <= 1 and len(q.split()) <= 5:
        faq = [h for h in a["pool"][:a["n_faq"]] if h.get("kind", "faq") == "faq"]
        if len(faq) >= 2:
            near = [h for h in faq if float(h["score"]) >= 0.40 and float(faq[0]["score"]) - float(h["score"]) <= 0.08]
            if len(near) >= 2:
                return {"reason": "the question is short and fits several different entries about equally", "options": [h["question"] for h in near[:3]]}
    return None


def _llm_part(llm, used: bool, capability: str | None, failure: str | None) -> dict:
    cfg = getattr(llm, "cfg", None)
    return {"enabled": bool(llm), "used": used, "capability": capability if used else None, "attempted": capability, "failure": failure,
            "provider": getattr(cfg, "provider", None) or None, "model": getattr(cfg, "model", None) or None}


def compose(a: dict, llm=None, similarity_floor: float = 0.0, sim=None) -> dict:
    llm = generate.as_llm(llm)
    pool, grid, parts, chosen, top_sim = a["pool"], a["grid"], a["parts"], a["chosen"], a["retrieval_similarity"]
    base = {k: a[k] for k in ("question", "parts", "retrieval_similarity", "retrieval_similarity_note", "answer_sufficiency", "faq_considered", "limits", "part_assessments")}

    def done(outcome: str, **kw) -> dict:
        return {**base, "outcome": outcome, "verification": None, "fallback": None, "llm": _llm_part(llm, False, None, None), **kw}

    if a["conflicts"]:  # D: the sources disagree about the same thing: both are named, none is chosen
        c = a["conflicts"][0]
        return done("conflict", answer=None, conflicts=a["conflicts"], evidence_class="contradiction", evidence_support=0.0, response_confidence="none", escalate=True, used_llm=False,
                    message=f"The sources disagree about this ({c['reason']}): {c['between'][0]} says \"{c['texts'][0][:120]}\" and {c['between'][1]} says \"{c['texts'][1][:120]}\". I will not choose between them. {ASK}")
    known = [{"part": parts[p], "text": grid[p][i]["sentence"], "reference": ev._label(pool[i]), "source": ev._ref(pool[i])} for p, i in chosen]
    if a["unanswered"]:
        what = "; ".join(a["unanswered"])
        if chosen:  # what is supported is returned, what is unknown is named
            lines = " ".join(f"{k['text']} [{n}]" for n, k in enumerate(known, 1))
            return done("partial", answer=None, partial_answer=lines, partial=known, unanswered_parts=a["unanswered"],
                        claims=[{"text": k["text"], "refs": [n], "source": k["source"].get("source"), "location": k["source"].get("location"), "supporting_text": k["text"]} for n, k in enumerate(known, 1)], evidence_class="partial", evidence_support=a["evidence_support"],
                        response_confidence="low", escalate=True, used_llm=False, sources=[{"ref": n, **k["source"], "text": k["text"]} for n, k in enumerate(known, 1)],
                        message=f"I can answer only part of this. What I found: {lines} Nothing available covers: {what}. {ASK}")
        ask = clarification(a)
        if ask:  # nothing answers it, and a question back can: say what is unclear, offer what it might mean
            opts = ("Did you mean: " + "; ".join(f"\"{o}\"" for o in ask["options"]) + "? ") if ask["options"] else ""
            return done("clarification", answer=None, partial=[], unanswered_parts=a["unanswered"], evidence_class=a["evidence_class"], evidence_support=0.0, response_confidence="none",
                        escalate=False, used_llm=False, clarification=ask, message=f"I need a little more detail to answer that: {ask['reason']}. {opts}Could you say more?")
        return done("insufficient", answer=None, partial=[], unanswered_parts=a["unanswered"], evidence_class=a["evidence_class"], evidence_support=0.0, response_confidence="none",
                    escalate=True, used_llm=False, message=f"The information I have does not answer this. Nothing available covers: {what}. {ASK}")

    best = a["best_faq"]
    strongest = best is not None and all(a["support"][p] and a["support"][p][0] == best for p in range(len(parts)))  # no other source says it better, part by part
    if best is not None and a["answer_sufficiency"] == 1.0 and strongest:  # A: one stored answer supports every part, and is the best support for each: returned as it is, unless a checked rewording is offered
        h = pool[best]
        out = done("faq", answer=h["answer"], sources=[{"ref": 1, **ev._ref(h)}], claims=[{"text": h["answer"], "refs": [1], "source": h.get("source") or "FAQ", "location": ev._ref(h)["location"], "supporting_text": h["answer"]}], evidence_class="semantic" if any(grid[p][best]["class"] == "semantic" for p in range(len(parts))) else "direct",
                   evidence_support=1.0, response_confidence="high" if top_sim >= similarity_floor else "medium", escalate=False, used_llm=False,
                   confidence_note="a category from the rules, not a calibrated probability")
        if llm is not None and llm.can("polish"):
            r = generate.polish(llm, a["question"], h["answer"], sim)
            out["llm"] = _llm_part(llm, r["text"] is not None, "polish", r["failure"])
            out["verification"] = r["verification"]
            if r["text"] is not None:
                out.update(answer=r["text"], answer_stored=h["answer"], used_llm=True)
            else:
                out["fallback"] = _fallback("the stored answer", r)
        return out
    sources, lines, used = [], [], {}  # B: assembled from what supports each part, with references
    for p, i in chosen:
        h = pool[i]
        sentence = span(a, p, i, parts[p])
        if h["entry_id"] not in used:
            used[h["entry_id"]] = len(sources) + 1
            sources.append({"ref": used[h["entry_id"]], **ev._ref(h), "text": sentence, "similarity": float(h["score"])})
        elif sentence not in sources[used[h["entry_id"]] - 1]["text"]:
            sources[used[h["entry_id"]] - 1]["text"] += " " + sentence
        also = [ev._ref(pool[j]) for j in a["support"][p][1:] if pool[j]["entry_id"] != h["entry_id"]][:2]  # other sources that say the same (a conflict would have been reported already)
        if also:
            sources[used[h["entry_id"]] - 1].setdefault("corroborated_by", [])
            sources[used[h["entry_id"]] - 1]["corroborated_by"] += [x for x in also if x not in sources[used[h["entry_id"]] - 1]["corroborated_by"]]
        lines.append((sentence, used[h["entry_id"]]))
        for extra, j in conditions_elsewhere(a, p, i, parts[p]):  # what another source adds about the same thing: its conditions and exceptions
            h2 = pool[j]
            if h2["entry_id"] not in used:
                used[h2["entry_id"]] = len(sources) + 1
                sources.append({"ref": used[h2["entry_id"]], **ev._ref(h2), "text": extra, "similarity": float(h2["score"])})
            lines.append((extra, used[h2["entry_id"]]))
    claims = [{"text": t, "refs": [r], "source": sources[r - 1].get("source"), "location": sources[r - 1].get("location"), "supporting_text": t} for t, r in dict.fromkeys(lines)]
    all_faq = all(s["kind"] == "faq" for s in sources)
    extractive = " ".join(f"{s} [{r}]" for s, r in dict.fromkeys(lines))
    needs_synthesis = len({r for _, r in lines}) > 1 and not all_faq
    answer, used_llm, report, fallback, part = extractive, False, None, None, _llm_part(llm, False, None, None)
    if needs_synthesis and llm is not None and llm.can("grounded"):  # the only other place a model is asked anything
        r = generate.combine(llm, a["question"], sources, sim)
        report, part = r["verification"], _llm_part(llm, r["text"] is not None, "grounded", r["failure"])
        if r["text"] is not None:
            answer, used_llm = r["text"], True
        else:
            fallback = _fallback("the passages' own sentences", r)
    semantic = a["evidence_class"] == "semantic"
    if used_llm and report:  # the model's words: each of its claims, with the sources it cites
        claims = [{"text": c["text"], "refs": c["refs"], "status": c["status"], "source": [sources[r - 1].get("source") for r in c["refs"] if 0 < r <= len(sources)]} for c in report["claims"]]
    return done("faq" if all_faq else "grounded", answer=answer, sources=sources, claims=claims, evidence_class=a["evidence_class"], evidence_support=1.0, verification=report, fallback=fallback, llm=part,
                response_confidence="medium" if len(sources) > 1 or semantic else "high" if top_sim >= similarity_floor else "medium", escalate=False, used_llm=used_llm,
                confidence_note="a category from the rules, not a calibrated probability",
                note=None if used_llm or not needs_synthesis else "assembled from the passages' own sentences; no model wording was used")


def _fallback(to: str, r: dict) -> dict:
    """What happened to a model's text that was not used, and what was said instead."""
    if r["failure"] == "failed_verification":
        types = sorted({i["type"] for i in (r["verification"] or {}).get("issues", [])})
        return {"from": f"the model's {r['capability']} text", "to": to, "reason": "failed_verification", "issues": types,
                "detail": "the model's wording did not stay inside the evidence (" + ", ".join(types) + f"), so {to} {'is' if to.startswith('the stored') else 'are'} used"}
    return {"from": f"the model's {r['capability']} text", "to": to, "reason": r["failure"], "issues": [], "detail": f"the model could not be used here ({r['failure']}), so {to} {'is' if to.startswith('the stored') else 'are'} used"}


def from_structured(results: list[tuple[str, dict]], model, base: dict, rest: dict | None, llm=None) -> dict:
    """What is said when part of a question (or all of it) was answered by structured reasoning over table rows (structured.py). `results` are (the part, its result); `rest`, if any, is the
    ordinary response to the other parts. Every sentence carries the references of the rows it was worked out from; what was left out or assumed is said in sentences of its own."""
    try:
        from . import structured
    except ImportError:
        import structured
    lines: list[tuple[str, list[str]]] = []
    computed = []
    for part, res in results:
        got, _ = structured.render(res)
        lines += got
        computed.append({"part": part, "operation": res["operation"], "values": res["values"], "numbers": res.get("numbers", []), "assumptions": [t for t, _ in res["assumptions"]], "not_used": [t for t, _ in res["gaps"]]})
    order = list(dict.fromkeys(e for _, refs in lines for e in refs))
    number = {e: n for n, e in enumerate(order, 1)}
    sources = []
    for e in order:
        hit = model.entries[e]
        used = [t for _, res in results for t in res["entries"].get(e, {}).get("sentences", [])]
        sources.append({"ref": number[e], **ev._ref(hit), "text": " ".join(dict.fromkeys(used))})
    text = [f"{t} " + "".join(f"[{number[e]}]" for e in refs) if refs else t for t, refs in lines]
    answered = [t for t, refs in lines if refs and not t.startswith(("Not used:", "Reading:"))]
    claims = [{"text": t, "refs": [number[e] for e in refs], "source": [sources[number[e] - 1]["source"] for e in refs], "location": [sources[number[e] - 1]["location"] for e in refs],
               "supporting_text": " ".join(sources[number[e] - 1]["text"] for e in refs)} for t, refs in lines if refs]
    common = {k: base[k] for k in ("question", "parts", "retrieval_similarity", "retrieval_similarity_note", "answer_sufficiency", "faq_considered", "limits", "part_assessments")}
    llm_part = _llm_part(generate.as_llm(llm), False, None, None)
    if not answered:  # the table has the column and not the value, or what was asked cannot be worked out from what is there
        why = " ".join(t for t, _ in lines)
        return {**common, "outcome": "insufficient", "answer": None, "partial": [], "unanswered_parts": [p for p, _ in results], "evidence_class": "related_insufficient", "evidence_support": 0.0,
                "response_confidence": "none", "escalate": True, "used_llm": False, "verification": None, "fallback": None, "llm": llm_part, "sources": sources, "computed": computed,
                "message": f"The information I have does not state this. {why} {ASK}"}
    if rest is not None and rest["outcome"] in ("insufficient", "clarification", "partial", "conflict"):  # the rest of the question has no answer: what was worked out is returned, the rest named
        left = rest.get("unanswered_parts") or rest.get("parts") or []
        extra = rest.get("partial_answer") or ""
        shift = len(sources)
        if extra:
            extra = re.sub(r"\[(\d+)\]", lambda m: f"[{int(m.group(1)) + shift}]", extra)
            sources += [{**s, "ref": s["ref"] + shift} for s in (rest.get("sources") or [])]
        body = " ".join(text) + (" " + extra if extra else "")
        what = "; ".join(left)
        return {**common, "outcome": "partial", "answer": None, "partial_answer": body, "partial": [], "unanswered_parts": left, "claims": claims, "evidence_class": "partial", "evidence_support": 0.5,
                "response_confidence": "low", "escalate": True, "used_llm": False, "verification": None, "fallback": None, "llm": llm_part, "sources": sources, "computed": computed,
                "message": f"I can answer only part of this. What I found: {body} Nothing available covers: {what}. {ASK}"}
    answer = " ".join(text)
    if rest is not None and rest.get("answer"):  # the other parts were answered the ordinary way: their sources follow ours
        shift = len(sources)
        extra = re.sub(r"\[(\d+)\]", lambda m: f"[{int(m.group(1)) + shift}]", rest["answer"])
        if rest["outcome"] == "faq" and "[" not in extra:
            extra += f" [{shift + 1}]"
        answer += " " + extra
        sources += [{**s, "ref": s["ref"] + shift} for s in (rest.get("sources") or [])]
        claims += [{**c, "refs": [r + shift for r in c.get("refs", [])]} for c in (rest.get("claims") or [])]
    return {**common, "outcome": "grounded", "answer": answer, "partial": [], "unanswered_parts": [], "claims": claims, "evidence_class": "computed", "evidence_support": 1.0, "response_confidence": "high",
            "escalate": False, "used_llm": False, "verification": None, "fallback": None, "llm": llm_part, "sources": sources, "computed": computed,
            "confidence_note": "worked out from the rows of the business's own tables; a category from the rules, not a calibrated probability"}
