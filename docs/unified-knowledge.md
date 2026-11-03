# One knowledge base, whatever the source

A business has a FAQ, a handbook, a price list, a spreadsheet of integrations, a JSON export of its offices and a website. A customer asks a question. The engine searches **all** of it, whichever source holds the evidence, decides whether the evidence is enough, and answers with a reference to where each fact came from, **without the customer knowing, or the engine caring, which kind of source it was**. This page is how that works, how it was measured, and where it still fails.

A FAQ is not a special case. It is one more kind of source, read by the same pipeline into the same kind of entry, searched in the same ranking, with **no ranking advantage** (a curated answer is used word for word only when it is the strongest support for what was asked).

```
 FAQ table · Markdown/text · PDF/Word · CSV/Excel · JSON · web pages · (anything a reader is written for)
        │ identify type → parse → normalise → chunk → keep source, heading and address → embed
        ▼
   ONE index of entries and their views (one vector matrix, one keyword index)
        │ question → embed → meaning + keywords, one ranking, current material only
        ▼
   RETRIEVAL (candidates, with scores)  →  EVIDENCE (what is actually supported, per part)  →  RESPONSE (words, references)
                                                                                                   └─ optional language model, then verification
```

## The pipeline, by kind of source

| Source | Read as | What each piece looks like |
|---|---|---|
| FAQ table (`.csv`, `.tsv`, `.xlsx`, `.json`, `.jsonl` with a question column; any header names) | FAQ entries, with alternative phrasings | question, answer; a curated answer, searched by its question, its phrasings and question + answer |
| Prose table (title and text columns) | documents | a passage per row, headed by its title |
| Table of values (a price list, a schedule, a product list, any sheet of a workbook) | records | a passage per row of **self-contained sentences**: `Pro plan, seats: 5.` The unit in a header goes with a bare number; a file's name is the context when a row does not say what it is (`Pune city (offices), address: …`) |
| Markdown, text, HTML, PDF, Word | documents | passages of about 80 words cut at headings, each remembering its file and section; tables inside a document are read like tables of values |
| JSON of any shape | records | a passage per object or record: nested objects flattened into sentences (`Limits, audit log retention: 365 days.`), lists read, records named by their name field |
| Web page, or a page and the pages it links to on the same site | documents | the same, with the page's own address as its source |

Columns are not assumed: what a column **holds** decides its role, and the Studio's *Columns* control overrides a wrong guess. A file of **held-out test questions** is refused as knowledge whatever its headers say. A file that cannot be read is reported with the reason and **does not disturb the others**.

## What is kept with every piece

| Kept | Hosted (database) | Local (Studio / package) |
|---|---|---|
| business | `business_id` on every row; composite foreign keys; row level security | one project = one business |
| source and its type | `knowledge_sources.source_type`, `name` | the file's record: name, kind |
| where it is | `url` or `storage_path`; a chunk's `location` (the page's own address, the section) | the entry's `source` (file name or page address) |
| the piece | `faqs` / `document_chunks`: `heading`, `content`, `content_hash`, `ordinal` | `Entry`: question or heading, answer or text |
| its vectors | `embeddings` (one per way of asking, per question-with-answer, per chunk) | `views.npy` |
| ingestion version and time | `source_version`, `created_at`; `knowledge_source_versions` | the file's `version` and history |
| freshness | `valid_from`, `valid_until`, `supersedes` on the source | the same three, from a header in the file |
| safety | `injection_flags` (instruction-like text is kept away from any model) | the same, at answer time |

`public.knowledge_units` (migration 09) shows all of it in one shape, for the people who own it.

## Retrieval

One list. A FAQ entry, a passage, a spreadsheet row, a JSON record and a web page are scored by the same blend of meaning (MiniLM embeddings) and keywords (BM25), the best view of each counting, and the best 10 returned (`docs/retrieval-architecture.md`). Nothing is added to a score for being a FAQ, or for being a document; this is tested both ways round. What is not current (expired, not yet valid, replaced) is not a candidate. Nobody's data is searched but the asking business's.

## Evidence and response across sources

Evidence judges each **part** of the question against each candidate's sentences and reports what is supported, how, and from where (`docs/evidence-architecture.md`). The same code handles:

| Situation | What happens |
|---|---|
| answer in one source | that sentence, quoted whole (conditions travel with it), with its reference; a stored FAQ answer verbatim when it is the strongest support |
| answer spread over several documents | each part supported by its best sentence, each cited; sources that say the same are listed as corroborating |
| FAQ and document together | combined the same way; the FAQ answer is one candidate among others |
| a question with several parts | each part judged on its own ("…and which plans have **it**" carries the name from the part before) |
| only part of it is known | the supported part is answered, the rest is named as not covered; nothing is made up for it |
| sources disagree | both are reported with their sources and sentences, neither chosen |
| nothing supports it | an honest "this does not answer it", a question back, or an offer to hand over to a person |
| only out-of-date material would | said so, with the source and why (`expired on …`, `superseded by …`); its facts are **not** given |

Attribution survives every step: a passage keeps its source and section through chunking, retrieval and assessment; the response lists, for each claim, the sentence it came from and the source it was read in; a language model's text is accepted only if every claim of it is still supported by those sentences (`docs/llm-integration.md`).

## Freshness

A source can say, in a header at the top of a Markdown or text file,

```
---
valid_until: 2026-12-31      # stops being current after this day
effective: 2026-01-01        # starts being current on this day (also valid_from)
supersedes: refund_2025.md   # the source(s) it replaces, separated by commas
---
```

or a person can set the same three things in the app. Dates are ISO (2026-12-31); anything else is ignored **with a note**, never guessed, and never stops the text from being read. A replacing source only retires the older one once it is itself in force, so a policy announced for next month does not retire this month's. The hosted service evaluates this per day, so a source expires at midnight with nobody touching it.

## Lifecycle

| | Hosted | Local |
|---|---|---|
| Add | upload / address → `ingest` reads, embeds, stores in one transaction | add files in the Studio |
| Re-upload | same bytes: nothing happens. Changes: the old reading becomes inactive history, the new one active, no duplicates, what changed recorded | same name replaces the file; a version and a note of what changed (`v2 · +1 −1 =3`) |
| Update web content | "Read again" | add the same address again |
| Delete a source | its FAQs, passages and vectors are deleted with it; answers from it stop | remove the file; gone after the index update |
| Re-index | automatic (the cached index notices) | **Update the search index**: seconds, no training; unchanged vectors kept |
| Failed reading | the source is marked failed; the last good reading stays searched; other sources untouched | the file is refused and the old one put back |
| Obsolete content | never a candidate; reported as out of date when only it would answer | the same |

## Without a language model

Everything above works with none: ingestion of every kind of source, embeddings, unified retrieval, evidence, supported answer composition, partial answers, clarification, abstention, references. A model, if the business turns one on, can only reword what evidence already established or suggest other searches; it never supplies a business fact, and its text is verified claim by claim against the evidence before use, with a recorded fallback (`docs/llm-integration.md`).

## How it was measured

**Sets.** `examples/unified/` is a fictional business (Acme Cloud) with 14 files of every kind: a FAQ, seven Markdown documents (one expired and one superseded on purpose, and a marketing page that contradicts the service-level document), a price table, a workbook with two sheets, two JSON files, two web pages. 98 questions across FAQ, document, website, CSV, Excel, JSON, multi-source, multi-part, partial, conflicting, outdated, unsupported and vague, split by a hash of the question id (`scripts/build_unified_examples.py`): **dev 48, regression 9, locked 41** (the locked file's sha1 is recorded; the benchmark refuses to show its questions one by one).

**Harness** (`scripts/unified_benchmark.py`), the same path as the hosted service, no language model. Five things are reported separately: retrieval (do the files that hold the evidence appear among the candidates?), evidence sufficiency (does it answer exactly the answerable questions?), answer correctness (right kind of response and the right facts), citation correctness (every file the facts came from is cited; none that must not be, such as an expired one), abstention (declines exactly the unanswerable).

**Order of work.** The harness was written and run on the code as it was **before** (`baseline`), then the code was changed, with the development set used for debugging; the regression set is a gate; the locked set was run **once**, at the end, for both versions, and nothing was changed afterwards.

### Results

Same questions, same harness, same pretrained MiniLM, no language model; the earlier code is a frozen copy of the source as it was before this work (`UNIFIED_SRC`). **The first column of each table is the answer to "what did the work change", the third is what the engine does now.**

**Locked set (41 questions, run once for each version, aggregate only):**

| locked | Earlier code | Final code |
|---|---|---|
| Evidence files found among the candidates (recall) | 92.7% | 100.0% |
| Every needed file found (multi-source) | 91.2% | 100.0% |
| Sufficiency precision | 87.5% | 96.3% |
| Sufficiency recall | 67.7% | 83.9% |
| **Correct answer rate (all questions)** | 58.5% | 78.0% |
| False answers (given and wrong) | 23.8% | 8.7% |
| **Citation correct** | 52.9% | 82.4% |
| Answers with every sentence cited | 85.7% | 87.0% |
| Abstention precision | 37.5% | 58.3% |
| Abstention recall | 85.7% | 100.0% |
| Conflicts detected | 33.3% | 66.7% |
| Out-of-date material used in an answer | 0 | 0 |
| Files that could not be read | 1 | 0 |

Correct answers by the kind of source the question needs (locked):

| Source type the answer needs | Earlier code | Final code |
|---|---|---|
| CSV | 5/6 | 5/6 |
| FAQ table | 4/7 | 5/7 |
| Web page | 4/4 | 4/4 |
| JSON | 1/4 | 3/4 |
| Markdown | 9/14 | 11/14 |
| Excel | 1/6 | 4/6 |

**Development set (48 questions, used for debugging, so optimistic):**

| dev | Earlier code | Final code |
|---|---|---|
| Evidence files found among the candidates (recall) | 90.0% | 100.0% |
| Every needed file found (multi-source) | 88.1% | 100.0% |
| Sufficiency precision | 100.0% | 97.6% |
| Sufficiency recall | 72.5% | 100.0% |
| **Correct answer rate (all questions)** | 60.4% | 93.8% |
| False answers (given and wrong) | 15.4% | 5.4% |
| **Citation correct** | 59.5% | 95.2% |
| Answers with every sentence cited | 88.5% | 86.5% |
| Abstention precision | 35.3% | 100.0% |
| Abstention recall | 100.0% | 100.0% |
| Conflicts detected | 0.0% | 50.0% |
| Out-of-date material used in an answer | 2 | 0 |
| Files that could not be read | 1 | 0 |

Correct answers by the kind of source the question needs (development):

| Source type the answer needs | Earlier code | Final code |
|---|---|---|
| CSV | 3/5 | 5/5 |
| FAQ table | 8/11 | 10/11 |
| Web page | 3/3 | 3/3 |
| JSON | 0/6 | 5/6 |
| Markdown | 11/19 | 17/19 |
| Excel | 1/6 | 5/6 |

**Regression set (9 questions, a gate rather than a result):** correct 66.7% before, 77.8% now; false answers 16.7% before, 14.3% now. Nothing that passed before fails now.

**How to read them.**

* The earlier code could not read `product_profile.json` at all (nested JSON was refused) and read only the first sheet of a workbook; JSON questions went from 0/6 to 5/6 and Excel from 1/6 to 5/6 on the development set, and from 1/4 to 3/4 and 1/6 to 4/6 on the locked set. Markdown went from 11/19 to 17/19 and FAQ from 8/11 to 10/11 on the development set (they were already read; what improved is which source wins and what counts as a conflict), CSV from 3/5 to 5/5.
* **Out-of-date material appearing in an answer or among the sources cited for it went from 2 to 0** on the development set. The three development questions about expired or replaced material were all failed before (two declined although a current document answers them; for the third the earlier code reported a "conflict" between the current service-level document and the superseded one and cited both) and are all answered from the current document now. On the locked set the count was 0 before and after, so this claim rests on the development set and on the tests (`tests/test_unified_ingestion.py`, `tests/test_unified_hosted.py`).
* **The locked result is 15 points lower than the development one (78.0% against 93.8%).** That gap is what tuning on a development set costs; it is the honest figure to quote. The development set is the one the fixes were made on; the locked questions were not looked at, and their per-question results still have not been.
* The change is large against the earlier code on every locked measure (correct 58.5% → 78.0%; false answers 23.8% → 8.7%; citations 52.9% → 82.4%), but 41 questions is small: one question is 2.4 points, and the categories have 2 to 6 questions each, so a category's number is a direction, not a measurement.
* Abstention precision (the share of "I cannot answer" that were right) is low on the locked set (58.3%): of the questions the engine declined, 4 in 10 could have been answered. Abstention recall is 100%: it declined every question that has no answer. When unsure, it errs towards declining.
* The earlier **evidence** benchmark (Pawfect, `examples/evidence/`) was re-run so that nothing it measured got worse: development and regression are identical to the earlier numbers, and its locked set (run once more, for reporting, aggregate only) came out at coverage 82.4%, correct 76.5%, false answers 7.1%, multi-part 2 of 2, partial handling 4 of 5, conflicts 3 of 3, against 76.5%, 70.6%, 7.7%, 1 of 2, 5 of 5, 3 of 3 at the previous milestone: one multi-part question that had been lost is answered again, and one partial-answer question that had been right is not. Net one more question correct; the cause of either was not investigated on the locked questions.


## Does fine-tuning on the FAQ change how passages are found?

The encoder that ships is chosen by held-out FAQ questions only, and passages are searched with the same encoder, so the choice never sees them. `scripts/unified_encoder_comparison.py` runs the same knowledge, retrieval, evidence and response code with the pretrained MiniLM and with three tuned encoders (two trained on the Pawfect FAQ, whose handbook is in the knowledge; one trained on the WASHO FAQ, a different business) on Acme (dev, regression) and on Pawfect (dev, regression).

| Encoder | Mean cosine to pretrained | Acme dev: correct / false answers | Acme regression | Pawfect dev | Pawfect regression |
|---|---|---|---|---|---|
| pretrained | — | 95.8% / 2.7% | 77.8% / 14.3% | 88.9% / 0.0% | 100.0% / 0.0% |
| pawfect-heldout | 0.999 | 95.8% / 2.7% | 77.8% / 14.3% | 88.9% / 0.0% | 100.0% / 0.0% |
| washo-improved | 0.9249 | 95.8% / 5.3% | 77.8% / 14.3% | 88.9% / 0.0% | 100.0% / 0.0% |
| evidence-kb | 0.9994 | 95.8% / 2.7% | 77.8% / 14.3% | 88.9% / 0.0% | 100.0% / 0.0% |

(Correct answer rate / false-answer rate; 'Mean cosine' is how close the tuned encoder's vectors for the Acme knowledge are to the pretrained encoder's. Pawfect's own tuned model is the first two rows after the pretrained one: the evidence run and the held-out run.)

No new model, reranker or rule was added on the strength of this: there is no evidence in it that tuning on a FAQ harms (or helps) passages here. It is small (57 Acme and 21 Pawfect questions, three tuned encoders, one of which barely moved), so it rules out nothing large and proves nothing small. The one encoder that moved a lot (mean cosine to the pretrained vectors 0.925, WASHO) changed one Acme answer's wording, not a result. If a business's tuned encoder moves that far, running this script on their own documents is the check.

> **Later:** the structured reasoning, claim verification and the model-independent pipeline built on this engine are in `docs/reasoning-pipeline.md`.

## Limits and failures

What does not work, in the order it matters. All of these are in the development or locked failures or follow from how the engine is built; none was hidden by changing a set.

1. **Questions that need a comparison or a calculation across rows** ("which plan is cheapest?", "how much do 3 seats cost?") are not answered: the engine finds and quotes sentences, it does not compute. It declines (the regression set keeps one such question and fails it).
2. **A vocabulary gap that the embedding model cannot bridge.** "Which plans have it?" against a column called "available on"; "who leads the company?" against "chief executive officer". The meaning check is MiniLM's, and where it does not see the link the part is reported as not covered (an honest decline or a partial answer, not a wrong answer).
3. **A sentence that shares the topic word but not the asked-for one can be accepted** ("annual billing is available on request" for "is there an annual discount?"). This is the one failure that gives a wrong answer rather than a decline; it is in the regression set and still fails. The false-answer rate on the locked set is 8.7%.
4. **Conflicts are found for numbers with units (prices, durations, times, percentages) and for yes against no about the same thing,** and only when both sources are close enough to the question to be candidates. A paraphrase can hide one (a question about "when invoices are due" finds a FAQ and a policy disagreeing; "the payment term for invoices" does not). Locked: 2 of 3 conflict questions detected.
5. **It declines more than it should when unsure** (abstention precision 58.3% on the locked set, recall 100%): it prefers "this does not answer it" to a guess.
6. **The gap between the development and locked numbers (93.8% against 78.0%)** is the cost of having tuned on the development set. The sets are small, written by one author, for one fictional business and in English.
7. **Freshness is only as good as what the sources say.** A header can state validity in a Markdown or text file; PDF, Word, HTML and tables carry none, so there a person has to set the dates in the hosted app. Nothing infers that a page is stale from its content, and there is no scheduled re-crawl (re-reading is a button). A web page's `last_crawled_at` is recorded; nothing acts on it.
8. **English only** for the stop words and the stemmer; other languages still match as written, more weakly.
9. **Hosted mode** is tested on a real PostgreSQL with the real embedding model, not on a live Supabase project, and the Vercel layer and the always-on engine container were not run (`docs/production-readiness.md`). No real language-model provider was called in any test or measurement.
10. **The tuned-encoder comparison** is small, as said above.


## Run it

```bash
.venv/bin/python scripts/build_unified_examples.py            # rewrites examples/unified (deterministic)
.venv/bin/python scripts/unified_benchmark.py --set dev --detail
.venv/bin/python scripts/unified_benchmark.py --set regression
UNIFIED_SRC=/path/to/older/src .venv/bin/python scripts/unified_benchmark.py --set dev   # the same measurement of an older version
.venv/bin/python scripts/unified_encoder_comparison.py --tuned runs/pawfect-heldout/model
.venv/bin/python scripts/unified_benchmark.py --set locked    # aggregate numbers only; once, at the end
```
