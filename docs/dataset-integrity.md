# Dataset integrity

A measurement is only worth reporting if nothing it measures was used to build or tune the thing being measured. This is the protocol, where each rule is enforced, and what is not covered.

## The three roles of question set

| Role | Used for | Never used for | Reported as |
|---|---|---|---|
| **Development** (and the validation questions generated from the FAQ) | debugging, choosing thresholds and the keyword share, retrieval and evidence work | the final claim | development numbers |
| **Deployment regression suite** | deciding whether a new version may replace the live one | being called a test set: **because it influences which version goes live, it is not independent** | "regression: passed / failed" |
| **Locked final benchmark** | the final report, once | tuning anything, choosing a model, promoting a release, being read question by question | aggregate numbers only |

In this repository: `examples/evidence/{dev,regression,locked}.csv` (the locked file's sha1 is in `locked.sha1`; `scripts/evidence_benchmark.py` refuses `--detail` on it), the held-out files of the Studio (`dev`/`test` split), and the three roles in the database (`evaluation_datasets.role`).

## The unified knowledge sets (`examples/unified/`)

A second set of questions for the whole knowledge base (FAQ, documents, spreadsheet, JSON, web pages, expired and replaced material): 98 questions, assigned to **dev (48), regression (9) and locked (41)** by a hash of the question id, so which set a question is in never depends on how it behaves. They are built by `scripts/build_unified_examples.py` and measured by `scripts/unified_benchmark.py`.

* The harness and the baseline measurement were written and run **before** any change to the code (`runs/unified/baseline.*.json`, produced from a frozen copy of the earlier source with `UNIFIED_SRC`). Two flaws of the harness itself found by that first run (a sentence splitter that miscounted citations; a `must_contain` word that matched by accident) were fixed in the harness, not in the code under test, and are noted.
* The development set was used to debug. The regression set is a gate: it must not get worse, and it is not reported as a result. The locked set has its sha1 in `examples/unified/locked.sha1`; `--detail` is refused for it; it was **run once, at the end, for the earlier and the final code, and never looked at question by question**.
* A planted ambiguity in the development data (one key called only `retention_days`, which a question about "retention" could mean either of two things) was removed by renaming the key in the data, before any locked result was seen. Nothing else in the data or questions was changed after results were seen.
* Questions the engine is not designed to answer (a question that needs a comparison across rows, such as "which plan is cheapest?") are **kept** in the sets and reported as failures, not removed.
* The same questions are asked of a second business's knowledge in `tests/test_unified_hosted.py` to measure leakage across businesses (zero sources, candidates or answers from the other business over all 98 questions).

## Where each rule is enforced

| Rule | Python (local) | SQL (hosted) |
|---|---|---|
| Held-out questions are never indexed as FAQ entries | a file of test questions is refused as a knowledge source by every reader; a project that already holds one is repaired (`tests/test_isolation.py`) | `guard_knowledge_text` trigger refuses an evaluation question as an FAQ question, alternative phrasing or learned question |
| Training/index/threshold choice cannot depend on the held-out file | train twice with different held-out files and compare the artifacts byte for byte (`tests/test_evaluation_integrity.py`) | — |
| A `split` column cannot make a test question a tuning question | every held-out row is test-only; the column is ignored (tested) | — |
| Exact duplicates | removed and counted, never used to pad a set (`tests/test_isolation.py`) | unique `(dataset, normalised question)`; the same question in another set is refused |
| Near-duplicates and paraphrases of a training phrasing | `heldout.py` compares every held-out question with every training phrasing and drops near-duplicates (same words, a typo, a light rewording), listing them | **not enforceable in SQL** (not an equality test); done in Python before loading |
| Paraphrases of one fact must not straddle sets | group by canonical FAQ identity when building sets (`scripts/build_evidence_sets.py`) | `group_key`: a group already in another set refuses a new question |
| Conflicting expected answers | every question needs a valid expectation; duplicates counted | duplicates refused, so two answers to one question cannot coexist |
| Evaluation labels are kept apart from knowledge | labels live in their own files/tables; sources never carry them | `evaluation_questions` is a separate table |
| Locked set cannot be tuned against | tool refuses per-question output; run once | sealed (`sealed_at`), immutable, results append-only and only `final_report`; locked questions visible to owners and admins only; a release gate may only be measured on the regression set |
| Feedback does not contaminate evaluation | feedback lives in `learned_questions` / the project's history and is used for retraining, never added to a test set | a learned question equal to an evaluation question is refused (and the reverse) |
| A new version replaces the live one only after the gate | `ci/improve.py` checks the regression suite and rolls back otherwise | `knowledge_versions`: active only with a passed `promotion_gate` result; `rollback_to_version` |

## What was done in this work

* No threshold, alpha, pattern or rule was chosen using the locked set. The locked benchmark was run again at the end **for reporting** and its result is worse than the previous milestone's; the cause was deliberately **not** investigated on the locked questions (see `docs/evidence-results.md`).
* The hybrid blend (keyword share 0.3) was chosen on the development questions of the nine retrieval datasets; the guard size likewise (`docs/retrieval-architecture.md`).
* The verification layer was developed and then fixed on the stored answers of the example knowledge bases; the same answers are used to report it. Those numbers are therefore **optimistic**; the first, unfixed run is reported next to them (`docs/llm-evaluation.md`).
* The evidence sets are small (36 locked, 6 regression, 15 development), hand-written by one author who also wrote the rules' vocabulary.

## Not covered

* Near-duplicate detection across **sets** in the hosted database (only exact and group rules are enforced there).
* Contamination through a language model's training data (a public FAQ may be in a model's memory); the local-only retrieval path has no such issue, a model's wording is checked against evidence, not against truth.
* Question sets written by the same person as the FAQ share their phrasing habits. Real customer questions would be a better held-out set than any generated one.
