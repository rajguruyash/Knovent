# Evidence-aware answering: before and after

> **Later work:** the same evidence engine now serves a knowledge base made of every kind of source at once, measured on a second family of sets (`docs/unified-knowledge.md`). The old sets below were re-run after those changes: development and regression are identical to the numbers here, and the locked set moved from 76.5% / 70.6% / 7.7% / 1 of 2 to 82.4% / 76.5% / 7.1% / 2 of 2 (coverage / correct / false answers / multi-part), with partial handling 5 of 5 → 4 of 5 (one question each way; neither was examined on the locked questions).

Same retrieval for both columns (one knowledge base run, `runs/evidence-kb`: the Pawfect FAQ and handbook plus a policy update that contradicts them on two points), same questions, same judging (`scripts/evidence_benchmark.py`). "Before" is the implementation frozen in `scripts/legacy_evidence_v1.py`; "after" is `runtime/evidence.py` + `runtime/response.py`.

## Three kinds of data, kept apart

| Data | Used for | Influences decisions? |
|---|---|---|
| Validation (generated from the FAQ; `examples/evidence/dev.csv` for the evidence layer) | choosing models, thresholds, the keyword blend; finding and fixing failures | yes, that is its job |
| Deployment regression suite (`regression.csv`) | checked before a new version replaces the live one (`ci/improve.py`) | **yes: it decides promotion, so it is not a test set** |
| Locked final benchmark (`locked.csv`, sha1 in `locked.sha1`) | reporting only; written before the analysis, its per-question results never viewed, run once at the end | no |

The evidence sets were written by hand from the three knowledge files and split by a hash of the question id into dev (15), regression (6) and locked (36). The Pawfect held-out file was read question by question for the miss analysis, so it is now a development set (a diagnostic), no longer a clean test. Settings were chosen by looking at dev, the regression suite and that diagnostic; the locked set was run once at the end.

## Locked final benchmark (36 questions)

| Metric | Before | After |
|---|---|---|
| Answer coverage (answerable questions that got an answer) | 71% | 82% |
| Correct answer rate (answered, with the right facts and sources) | 65% | 76% |
| False-answer rate (wrong answers among all answers given) | 8% | 7% |
| Abstention precision (declines that were right) | 79% | 79% |
| Abstention recall (questions to decline that were declined) | 100% | 100% |
| Evidence-sufficiency precision | 100% | 100% |
| Evidence-sufficiency recall | 71% | 82% |
| Multi-part completion (all parts answered correctly) | 50% | 100% |
| Partial handling (supported part returned, unknown part named) | 0% | 100% |
| Conflict detection | 100% | 100% |

Correct by kind of question: direct 6/6 → 6/6; paraphrase 4/9 → 5/9; partial 0/5 → 5/5; related 6/6 → 6/6; unsupported 5/5 → 5/5; multi-part 1/2 → 2/2; conflict 3/3 → 3/3

## Regression suite (6 questions)

| Metric | Before | After |
|---|---|---|
| Answer coverage (answerable questions that got an answer) | 75% | 100% |
| Correct answer rate (answered, with the right facts and sources) | 75% | 100% |
| False-answer rate (wrong answers among all answers given) | 0% | 0% |
| Abstention precision (declines that were right) | 100% | 100% |
| Abstention recall (questions to decline that were declined) | 100% | 100% |
| Evidence-sufficiency precision | 100% | 100% |
| Evidence-sufficiency recall | 75% | 100% |
| Multi-part completion (all parts answered correctly) | 50% | 100% |
| Partial handling (supported part returned, unknown part named) | n/a | n/a |
| Conflict detection | n/a | n/a |

Correct by kind of question: direct 1/1 → 1/1; paraphrase 1/1 → 1/1; related 2/2 → 2/2; multi-part 1/2 → 2/2

## Dev set (15 questions)

| Metric | Before | After |
|---|---|---|
| Answer coverage (answerable questions that got an answer) | 56% | 89% |
| Correct answer rate (answered, with the right facts and sources) | 44% | 89% |
| False-answer rate (wrong answers among all answers given) | 17% | 0% |
| Abstention precision (declines that were right) | 50% | 75% |
| Abstention recall (questions to decline that were declined) | 100% | 100% |
| Evidence-sufficiency precision | 83% | 100% |
| Evidence-sufficiency recall | 56% | 89% |
| Multi-part completion (all parts answered correctly) | 0% | 100% |
| Partial handling (supported part returned, unknown part named) | 0% | 100% |
| Conflict detection | 50% | 100% |

Correct by kind of question: direct 3/3 → 3/3; paraphrase 1/4 → 3/4; partial 0/1 → 1/1; related 2/2 → 2/2; unsupported 1/1 → 1/1; multi-part 0/2 → 2/2; conflict 1/2 → 2/2

## Pawfect held-out questions (diagnostic set, 121 unique questions: 90 answerable, 31 to decline)

| | Before | After |
|---|---|---|
| Answered correctly | 35 | 44 |
| Missed (declined or partly answered when answerable) | 48 | 41 |
| Answered, but from the wrong source | 6 | 3 |
| Stored FAQ answer for a question only the handbook answers | 1 | 0 |
| Answered only in part (supported part returned) | 0 | 2 |
| Correct abstentions (of 31) | 30 | 31 |
| Fabricated answers | 1 | 0 |

The "seven questions not accounted for" were the answerable questions that were *answered, but wrongly*: 6 from a source other than the expected one and 1 a stored FAQ answer for a question only the handbook answers. 35 correct + 48 missed + 7 wrong = 90; the earlier summary reported only the first two. Some "wrong source" cases are the labels, not the engine: "Is the grooming place in Viman Nagar?" is labelled with the location FAQ, but the home-visits FAQ also states the location. Every miss with its retrieved entries, similarity, per-part sufficiency decision and reason is in [evidence-miss-analysis.before.md](evidence-miss-analysis.before.md) and [evidence-miss-analysis.after.md](evidence-miss-analysis.after.md).

## What was wrong, and what changed

Before, 41 of the 55 failures were classed *paraphrase mismatch*. They were not all the model's fault; the assessment itself was defective:

- **Detail kinds were mis-detected**: "how much time" was read as a price, "open wound" and "when they arrive" as opening hours, so correct answers were refused for lacking a price or a time. Fixed, and a question that mentions a figure ("at 8 AM") now needs a figure of that kind in the answer.
- **The words that name the asked-for detail** ("area", "charge", "hours") were required to appear in the answer; they are now judged by the detail check instead.
- **Words that are in most entries** ("dog", "appointment") counted as evidence; they no longer do, and the entry's own subject words are not demanded of its answer.
- **Different wording was only recognised by a similarity threshold applied to one sentence.** There are now four classes per part (direct, semantic, related, none). A meaning-only match needs a second independent sign (some of the question's words, or the entry having been retrieved close to that part) and has to stand out from every other entry's best sentence; a direct statement elsewhere beats it.
- **Neighbouring sentences and pronouns**: "It does not groom cats" is now read together with the sentence that says what "it" is; word endings (medicine / medication) match.
- **Multi-part questions** are split into components (also "hours and address"), each is supported by the best entry, whichever FAQ entry or passage that is; an unsupported component is named and the supported ones are returned (outcome `partial`).
- **Conflicts** are looked for between any two sources that speak of the same thing, including a FAQ answer that supports the question only loosely.
- **Generation**: a model's rewording is kept when each sentence is supported by a source sentence in words or in meaning; any number, price, time or date that is not in the sources, or any unsupported sentence, discards it.

## Architecture

Three layers that only exchange plain data, so any one can be replaced (another embedding model, a reranker, a different LLM) without touching the others: **retrieval** (`Engine.candidates` / `ProjectEngine.candidates`: best FAQ entries and best passages with scores) → **evidence** (`evidence.assess`: per-part classes, support, conflicts, separate signals; it writes no answer) → **response** (`response.compose`: the stored answer, assembled evidence with references, a partial answer, a conflict report or an honest decline). `evidence.respond` runs the last two back to back.

## Limits (what this does not do)

- 33 of the 90 answerable Pawfect questions are still declined because they are paraphrases the assessment cannot connect ("Is it possible to turn up without booking?" for a FAQ about walk-ins). The embedding model's sentence-level similarity is not sharp enough to separate such a paraphrase from a related-but-wrong sentence, and the safe behaviour is to decline. This is where a reranker or cross-encoder in the retrieval/evidence layers would help.
- The sets are small (36 locked questions: one question is about 3 points) and written by one author who also wrote the rules' vocabulary. Treat the differences as direction, not precision.
- Thresholds for the semantic path (0.45 meaning similarity, +0.10 with no shared words, a margin of 0.08, 0.5 closeness) were set by looking at dev, the regression suite and the diagnostic set; they are not a fitted optimum.
- Sufficiency is judged from wording, figures and sentence-level similarity; it can accept a sentence that is close in meaning but says something else, and conflicts are found only for numbers with units and yes/no statements.

---

# Update: the production-readiness work (2026-11-01)

This section is added to the document above, which is kept as it was. Same question sets, same judge (`scripts/evidence_benchmark.py`); the difference is the code under test: the whole pipeline as the Studio and the hosted service run it (hybrid retrieval with the guard, evidence, response, **no language model**) instead of the evidence module on embedding-only candidates.

| Set | Questions | What changed against the numbers above |
|---|---|---|
| Development | 15 | **nothing**: every metric and every category identical |
| Deployment regression suite | 6 | **nothing**: identical (this is the promotion gate, not a test set) |
| Pawfect held-out diagnostic (development, read question by question in the earlier analysis) | 121 | **nothing**: 44 answered correctly, 31 correct abstentions, 41 missed, 3 answered from the wrong source, 2 partial, under both embedding-only and hybrid retrieval |
| **Locked final benchmark** (reporting only) | 36 | **worse by one question**, see below |

## Locked benchmark

| Metric | Previous milestone ("After" above) | Now |
|---|---|---|
| Answer coverage | 82% (14 of 17 answerable) | **76.5%** (13 of 17) |
| Correct answer rate | 76% | **70.6%** |
| False-answer rate | 7% | **7.7%** (1 wrong among 13 answers) |
| Abstention precision | 79% | 78.6% |
| Abstention recall | 100% | 100% |
| Evidence-sufficiency precision / recall | 100% / 82% | 100% / 76.5% |
| Multi-part completion | 100% (2 of 2) | **50%** (1 of 2) |
| Partial handling | 100% | 100% |
| Conflict detection | 100% | 100% |

Correct by kind: direct 6/6 · paraphrase 5/9 · partial 5/5 · related 6/6 · unsupported 5/5 · multi-part 1/2 · conflict 3/3.

**This is a decline, not an improvement.** One multi-part question that was answered correctly is not any more; it accounts for the whole difference in coverage, correctness and multi-part completion. A 36-question set cannot say whether that is noise (one question is 2.8 points) or a real fault, and **its cause was deliberately not investigated on the locked questions**: trying changes until the locked number recovers is exactly how a locked benchmark stops being independent. It was run twice in this work (before and after the retrieval guard was added), with identical results.

What changed between the two figures, and so what the cause must be among:

1. retrieval: hybrid keyword + embedding ranking with the guard (the evidence pool is the model's best four plus the blend's best);
2. evidence: *7 business days / 2 weeks / 3 months* count as durations; *how long / how soon* no longer demand the word "long"; *pounds* count as prices; a "related" source must share a specific subject word to count as a possible conflict;
3. the pipeline around it (quarantine of instruction-like passages, which flagged nothing in the evidence knowledge base).

Each of 2 was made because a test showed a real failure (a refund-time question could not be answered from "processed within 7 business days"; the bike shop's repair time was reported as contradicting its delivery time). Reverting any of them to find the culprit would mean choosing by the locked number. **Recommended next step:** write a new, larger development set (60 or more questions, especially multi-part and duration questions, written by someone other than the rules' author), find the failing case there, fix it on principle, and keep the regression suite as the gate.

## What the other numbers are, and are not

* "Hybrid is the best retrieval baseline" is a statement about **ranking** on nine retrieval datasets (`docs/retrieval-architecture.md`). It is **not** shown to improve evidence-based answers on these sets: dev, regression and the diagnostic are unchanged and locked is one question worse.
* The language-model layer is not part of these numbers (it does not run in them). Its checking is in `docs/llm-evaluation.md`.
* All sets here were written by one author and are small. Do not read two points as meaningful.
