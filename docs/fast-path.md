# The deterministic fast path

**One rule: do not ask a model for an answer the engine already has, complete and verified.** It is a small, auditable addition to the existing pipeline (`Pipeline.evaluate_fast_path` and `conclude_fast` in `runtime/pipeline.py`, about 90 lines), not a second retrieval system: it reads what `prepare` already established (query understanding, unified retrieval, evidence validation, sufficiency, structured reasoning) and can only **refuse to skip**. It changes no consent, no fallback permission, no model selection and no customer setting. Local inference stays the primary AI mode for every question that needs a model.

## When the model is skipped

All of this must hold, or the request continues through the pipeline exactly as it would have without a fast path:

| # | Condition | Where it comes from |
|---|---|---|
| 1 | The engine's own result is an **exact FAQ answer** (`faq`, evidence class `direct`) or a **fully supported structured answer** (`grounded`, class `computed`: a calculation, lookup, count or comparison worked out in code) | the existing evidence classification; a match by meaning (`semantic`), a passage, a partial answer, a conflict, a clarification or no evidence is never skipped past |
| 2 | The question was understood and nothing is unresolved: no clarification needed, no accepted sources disagree, no part left unanswered | the engine's own `clarification`, `conflicts`, `unanswered_parts` |
| 3 | Sufficiency, **assessed again without the engine's own override**, is `SUFFICIENT`: every requirement of the plan (every part of a multi-part question) is established by accepted evidence; nothing unsupported, nothing unverified, nothing left for a second retrieval | `sufficiency.assess` |
| 4 | Every piece of evidence the answer cites was **accepted** by validation, is **current** (not expired, not yet valid, not replaced) and belongs to **this tenant** | `evidence_check` results |
| 5 | The deterministic composition has at least one claim, **every claim cites evidence**, and it passes the same **claim and citation verification** every answer passes | `claimcheck.check` |

A similarity score is **never** an input (a test asserts the deciding code does not read one, and that a 0.999 similarity does not turn a meaning match into an exact one). A decline records why (`declined:evidence_not_accepted`, `not_eligible:conflict/contradiction`…). Missing information stays a deterministic abstention: no model is asked to fill a gap, because a model "cannot add what the evidence does not hold".

## What is new, and what is only now said out loud

* **New:** an exact FAQ answer no longer goes to the model. (Before, every FAQ question in an AI mode was sent to the provider, and a small model made some of those answers worse; see the numbers below.)
* **Already so, now labelled:** structured answers (arithmetic, counting, sorting, filtering, table lookups) were never sent to a model: `ModelComposer` returns the code-computed composition. The fast path puts the same five conditions in front of them and records `fast_path` when they hold; when they do not (for example, a comparison across a whole table whose rows validation accepted only as context) the answer is still computed in code, never by a model, and is labelled `deterministic` with the reason it was not called the fast path.

It applies to every step that can reach a model (server-side steps, and the two-phase local flow: `local_prepare` returns the finished answer and no request is left waiting for the person's computer). It is a switch of the operator: `ENGINE_FAST_PATH=on|off` (default on; off restores the previous behaviour exactly).

## What every answer now records (`execution`)

| Field | Meaning |
|---|---|
| `answer_path` | `fast_path` (a complete verified answer existed, the model was skipped), `model`, `deterministic` (a deterministic business, or computed in code), `abstained` (deterministic abstention: the evidence does not answer), `deterministic_fallback`, `deterministic_no_consent`, `unavailable`, `mock` |
| `fast_path_reason` | taken: `exact_faq_direct` or `structured_computed`; not taken: `declined:<why>`, `not_eligible:<outcome>/<class>`, `not_applicable:deterministic_mode`, `disabled` |
| `model_invoked` | whether a provider or the person's computer was actually asked |
| `provider_steps_tried` | the steps that really reached a model, in order (`[]` for a fast answer, a refusal or an abstention) |
| `evidence_ids` | the evidence the final claims cite |
| `verification` | `{outcome, passed, claims_checked, claims_removed}` |
| `latency_ms` | the pipeline's own total |

They are in the History tab's records, the structured log line (`answer_path`) and a counter per path (`answer_path_fast_path`…); no question text or evidence text is recorded in any of them.

## Measured against the deterministic baseline

`scripts/fast_path_report.py`, development, regression and fresh sets of the two fictional businesses (174 questions; the sealed locked, verify and holdout sets are not readable by the script). The baseline is the existing deterministic pipeline scored by the project's own judge. Raw: `runs/fast-path/report.json`, `report-with-model.json`.

| | Questions | Correct | False answers (given, and wrong) | Citation correct |
|---|---|---|---|---|
| **Baseline**, all 174 | 174 | 93.7% | 4.6% | 96.6% |
| &nbsp;&nbsp;development and regression sets (used to build the engine) | 134 | 100% | 0% | 100% |
| &nbsp;&nbsp;fresh sets (model-selection data) | 40 | 72.5% | 19.4% | 85.3% |
| **Fast path: questions that skipped the model** | 65 (37.4%) | **64 (98.5%)** | **1 (1.5%)** | **100%** |

* **All 65 are text-for-text identical to the baseline's answer and cite the same sources, all verified, and the model was invoked for none.** The fast path never changes an answer; it only chooses not to ask.
* **Of the 65, only 7 are exact FAQ answers (the genuinely new skip); 58 are structured answers that never went to a model.** By set: Acme dev 13 (4 FAQ), Acme fresh 8 (1), Beacon dev 27 (1), Beacon regression 10 (1), Beacon fresh 7 (0); Acme regression 0.
* **The one wrong fast answer** is a structured answer to a **fresh** question the knowledge cannot answer ("What would 200 seats cost on the Enterprise plan?": the structured reasoner multiplies a plan price by a quantity the table never prices). **The baseline gives the same wrong answer today** (a model is not asked about structured answers either), so the fast path did not introduce it, and it cannot fix it: the fix belongs in the structured reasoner (refuse a calculation whose rate the evidence does not state). **No rule was added for it:** it came from the fresh sets, which are model-selection data, and a condition tuned on them would make them (and any number I quote from them) worthless.
* Why not more: of the 174 questions the decisions were: structured (taken) 58; exact FAQ (taken) 7; passages the engine rates direct (not eligible, they are composed from passages) 32; passages rated by meaning 25; related-but-insufficient (abstentions) 21; partial 10; conflict 8; structured answers declined by the stricter validation or sufficiency check 7; FAQ by meaning 2; clarification 3; no evidence 1. The fast path is deliberately narrow.

**Latency** (this 8 GB laptop, `scripts/fast_path_report.py` run with nothing else heavy): a fast-path answer takes a median **31.6 ms** (p95 54.7 ms), the same as the deterministic pipeline (32.9 ms): **the decision itself costs 0.1 ms.** (A second run with Ollama loaded measured 157 ms against 153 ms for the same two paths: the machine was busier, the difference between the paths the same.)

**Against a real local model on the 7 exact FAQ answers** (`llama3.2:1b`, already installed, fast path off; the same questions the fast path answered in 25–260 ms):

| | Fast path | `llama3.2:1b` through the local pipeline |
|---|---|---|
| Correct and cited | 7 of 7 | 5 of 7 (the 5 are the deterministic answer, after the model failed verification) |
| Answered by the model | 0 | 2 of 7 (both reported a false `conflict`, so both were judged wrong) |
| Fell back to the deterministic answer | 0 | 5 of 7 (`reasoning:unsupported_claims` three times, `invalid_json` twice) |
| False answers | 0 | 0 (verification caught every bad answer) |
| Time | 25–260 ms | median 9.1 s; two answers took **about 185 s** each |

In other words: **the model path never beat the exact answer, was wrong or unusable five times in seven, and cost seconds to minutes.** Seven questions on one 1B model are a demonstration, not a benchmark, and a larger model may do better; the fast path does not depend on it.

## Honest limits

* The fast path is only as good as the engine's own classification and structured reasoning; it adds checks, not intelligence (above: one wrong structured answer is inherited). It declines more than it takes.
* The independent sufficiency check is weaker for structured answers (a structured operation that produced a result counts as establishing what was asked, by design in `sufficiency.assess`): a lexical "the asked attribute appears in the evidence" check was tried and **rejected**, because it dropped 19 of 36 correct structured answers (it does not know that "cost" and "price" are one thing).
* Document passages the engine rates `direct`, and FAQ matches by meaning, are **not** fast-pathed: they go to the model as before, which is the conservative choice and also why coverage is 37% and not higher.
* A related, pre-existing behaviour, seen in these tests and **not changed**: when a model composes an answer and says `answered`, the pipeline takes that verdict over the engine's own `partial` or `conflict` as long as every claim it makes is supported; a model can therefore drop a conflict or a missing part without failing verification. The fast path avoids it only for the exact answers it covers.
* The Studio (`faqengine studio`) does not use the fast path; it is wired into the hosted service and the local-first flow only.

## Tests

`tests/test_fast_path.py` (28; real components, real example knowledge, a provider that counts its calls): exact FAQ, the FAQ's own alternative phrasing, a meaning match, a loose paraphrase that stays an abstention, conflicting sources, outdated information, a multi-part question with every part covered and one with a part missing, a calculation, a structured answer the check declines, insufficient evidence, a clarification, a very high similarity not mattering, **each of the eight conditions alone stopping the fast path**, evidence of another tenant, a failed verification, the switch, a deterministic business, a test double's composer, and the execution record. `tests/test_fast_path_hosted.py` (12; real schema, real provider over HTTP): the provider receives nothing for an exact answer and one call for a question that needs a model; consent is still enforced where a model is needed and not where nothing is sent; withdrawal still stops the next call; no setting changes; tenant isolation (each business gets only its own answer through the fast path); an expired entry is not served although it matched exactly; missing information stays an abstention; the two-phase local flow does not ask the person's computer for an answer that exists; the History shows the path. The 53 existing tests that push FAQ questions through the model to test consent, routing, limits and metering now build their engine with `fast_path=False` (stated at each construction); their assertions are unchanged.
