# faq-engine (working name)

Train a custom FAQ retrieval engine on your own machine, measure whether it beats off-the-shelf search on
questions it has never seen, and export it as a package that runs anywhere without this tool.

This is **Phase 0**: a command-line pipeline and a benchmark, built first because the product's central
claim ("a model trained on your FAQ beats a generic one") had to be tested before a studio UI is built
around it. The answer is in [docs/RESULTS.md](docs/RESULTS.md): with 2 to 8 phrasings per FAQ entry, tuning
raised Hit@1 by 2.6 to 3.8 points over the untuned model (better in 9 of 9 runs), with 1 phrasing it did nothing, and
"answer directly without the LLM" was not established. The study of vaizent.com that the product is modelled on,
and the roadmap, are in [docs/REFERENCE.md](docs/REFERENCE.md).

## The website: upload a FAQ, train, test, approve or reject

Double-click `Start FAQ Studio.command` (or run `faqengine studio`). It opens http://127.0.0.1:8765, which only this computer can reach.

1. **Upload** a FAQ (`.csv`, `.tsv`, `.xlsx`, `.json`, `.jsonl`) or try the bike-shop example. A FAQ is all that is needed, but you can choose several files at once, or add more later on the Files tab: more FAQ tables, and documents (`.txt`, `.md`, `.html`, `.pdf`, `.docx`) that give the engine more context to retrieve from, or the address of a web page (and, if you like, the pages it links to), which is read and kept as text (see "Several inputs" below).
2. **Train.** Live stages, loss, examples per second, memory and time left, plus this computer's CPU, memory, disk and GPU. Training runs in its own process, so the page stays responsive and the memory is returned afterwards.
3. **Results.** Right-first, P@3/R@3, P@5/R@5, MRR@10 and nDCG@10 for keyword search, the starting model and your engine; loss (train, dev, check); scores by kind of question; how well off-topic questions are refused; your FAQ drawn as a map of embeddings; entries likely to be confused; model size and speed.
4. **Test.** Ask anything. See the closest entries (FAQ entries and document passages, labelled) with scores, the stored answer, and which of three outcomes it is (answer directly, needs a closer look, not in the FAQ). Approve or reject with a comment, and say what it should have matched. Your judgements give a running accuracy with its uncertainty, decide when answering without an LLM is proven safe, can be downloaded, and can be used in the next training run. Export a package that runs anywhere.

With only a FAQ file there are no real customer questions to check against, so the checks are generated from the FAQ (see `src/faqengine/selfcheck.py`) and the page says so everywhere it shows them: they find problems but cannot prove accuracy. Your judgements are the real measure. If you have written test questions of your own, add them as a held-out file (below) and the report measures those instead, separately from anything the model trained on.

The confidence lines are configurable: on the Test tab, set where "not in the FAQ" starts and where the engine may answer without an LLM. Left empty, they are calculated and the report says how; set by hand, they override the calculation (and "Back to automatic" returns to it).

The same thing from the command line: `faqengine auto --kb faq.csv --out runs/my-faq --export dist/my-faq`.

## Quick start (command line, with your own dev and test questions)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

faqengine check    --kb examples/bike-shop/faq.csv
faqengine train    --kb examples/bike-shop/faq.csv --dev examples/bike-shop/dev.csv --out runs/bike
faqengine evaluate --run runs/bike --kb examples/bike-shop/faq.csv --dev examples/bike-shop/dev.csv --test examples/bike-shop/test.csv
faqengine export   --run runs/bike --kb examples/bike-shop/faq.csv --dev examples/bike-shop/dev.csv --test examples/bike-shop/test.csv --out dist/bike-shop --target-precision 0.9
faqengine chat     --package dist/bike-shop      # type questions and see what it would do; no LLM or API key involved
```

`--target-precision` is how often an answer given without asking an LLM must be right. The default is 0.95;
the example has only 60 held-out questions, too few to prove that, so it uses 0.9.

`export` writes `dist/bike-shop/` and `dist/bike-shop.zip`. The package runs with only numpy, onnxruntime and
tokenizers; see its own README. To check a package in isolation (no PyTorch, no faqengine installed):

```bash
python scripts/check_package.py dist/bike-shop.zip examples/bike-shop/test.csv
```

Training uses a GPU when there is one (CUDA, or Apple's MPS) and falls back to CPU (`--device cpu`). On an 8 GB fanless laptop a small FAQ trains in one to four minutes, but
the speed varies about fivefold with heat and memory pressure (see the last section of docs/RESULTS.md).

## Your data

**FAQ file** (`.csv`, `.tsv`, `.xlsx`, `.json` or `.jsonl`): `question` (required), `answer`, `id`, `category`, and optionally
`alt_questions` (other ways customers ask it; in CSV separated by `|`). More phrasings per entry is the single
biggest lever on quality (see the results).

**Question files** (`.csv`): `query,entry_id`. A blank, `-` or `oos` entry_id marks a question the FAQ cannot
answer, which is how out-of-scope behaviour is measured.

- `--dev`: held-out questions, used to stop training early, to choose thresholds, and to refuse to ship a
  model that is not better than the untuned one. Aim for 3 to 5 per entry; fewer than about 50 in total cannot
  certify a high direct-answer precision.
- `--test`: questions the pipeline has never seen, used only for the report.

## Several inputs: a FAQ, documents and web pages, as one knowledge base

This is a **unified knowledge engine**, not a FAQ matcher: every source (a FAQ, Markdown, PDF/Word, CSV/Excel, JSON, web pages) goes through one pipeline into one index, a customer's question is answered from whichever sources hold the evidence, each fact is cited to where it came from, a FAQ gets no ranking advantage, and material that has expired or been replaced is never used as if it were current. How it works, how it was measured and where it still fails: [docs/unified-knowledge.md](docs/unified-knowledge.md).

Nothing here is specific to one business. The engine reads whatever it is given, from any mix of these, and the small embedding model (`all-MiniLM-L6-v2`) searches them all together:

- **Tables** (`.csv`, `.tsv`, `.xlsx`, `.json`, `.jsonl`): the header names do not have to match anything. Known names in many languages are used first (`Question`, `Q`, `Prompt`, `Pregunta`...; `Answer`, `Reply`...). Otherwise what the columns **hold** decides: a column whose values are questions is the question column, the longest prose column is the answer or the text, a short column of mostly different values names a row. A table with questions is read as FAQ entries; a table of prose (title and text) as documents; and a table of **values** (a pricing table, a product list, a schedule) row by row, every sentence naming its row (`Pro plan, monthly price: 20 USD.`, with a unit in the header such as `price_usd` going with a bare number), so a question about one plan finds one sentence with the plan, the attribute and the figure. If the guess is wrong, **Columns** beside the file in the Studio lets you choose how rows are read and which column plays which part: the uploaded file is kept untouched and the project reads a copy with standard headers, so training, export and retraining need nothing else. A file of **test questions** is still refused as knowledge, whatever its headers are called.
- **Documents** (`.txt`, `.md`, `.html`, `.pdf`, `.docx`): cut at headings, then into passages of about 80 words, each remembering its file. A header at the top of a Markdown or text file can state `valid_until`, `effective` and `supersedes`; a source that has expired, not started or been replaced is never used as current. They are searched together with the FAQ, so a customer's question can find a passage that the FAQ never covered.
- **Web pages**: paste an address, or choose "this page and up to 4 / 15 / 30 pages it links to on the same site". The page is read once (no JavaScript is run), its menus, footers and forms are left out, and the text is kept in the project as a small clean copy that remembers its address, then cut into passages like any other document. Add the same address again to read it afresh. It identifies itself, obeys the site's `robots.txt`, waits between pages, stays on one site, and never connects to an address on this computer or its network. A page that builds itself with JavaScript, needs a login, or asks not to be read is refused with the reason (save it from your browser and upload it as a file instead).
- A passage is **context, never a curated answer**: it is never returned as a direct answer (in the website or in the exported package), it is shown with its file name, and training uses only the FAQ entries. A project of documents alone works too; there is nothing to train, so the base model searches them and the report says so.

## Evaluation integrity: train, validation and test never mix

| Set | Where it comes from | Used for |
|---|---|---|
| Train | your FAQ's phrasings (and phrasings you approved) | fine-tuning, the index, the keyword vocabulary |
| Validation | generated from your FAQ (a few phrasings held back, typos and shortened questions, answer keywords, generic off-topic questions) | stopping training, choosing between the pretrained and the fine-tuned model, placing the "not in the FAQ" line, choosing the keyword blend |
| Test | your held-out file, **every row** | measuring, nothing else |

A held-out file can never become knowledge: a file whose columns say what the right answer is (`entry_id`, `expected_*`, `split`, ...) is refused as a FAQ or document source (and moved out of an old project that already holds one). Repeated questions are removed and counted, never kept to pad the set, and the same question with two expectations is an error. Test questions that copy a training or validation question are dropped and listed. `tests/test_isolation.py` and `tests/test_evaluation_integrity.py` prove it, the latter by training twice with two different held-out files and checking that the index, vectors, weights, thresholds and training log are identical.

The report compares the pretrained model, the fine-tuned model and keyword search on the same test questions, by kind of question (paraphrases, typos, short questions, answer keywords, multi-part, ambiguous, related-but-unanswered, unrelated, other languages), gives train, validation and test loss separately, shows how far fine-tuning moved the weights, runs the keyword/model/hybrid experiment (blend chosen on validation only), and judges the evidence-aware answers (below). Use `--no-finetune` (or the Files tab switch) for the pretrained control.

## Three independent layers: find, verify, answer

1. **Retrieval** (`runtime/retrieval.py`, `Engine.candidates`): finds the best entries of every kind (FAQ entries, passages, spreadsheet rows, JSON records, web pages) in one ranking by a hybrid of MiniLM embeddings and keywords, and returns every candidate with its similarity, keyword and combined scores. A cross-encoder reranker was tested and is **not** used ([docs/cross-encoder-experiment.md](docs/cross-encoder-experiment.md)).
2. **Evidence** (`evidence.assess`): decides whether what was found supports an answer, part by part, as plain data (direct / semantic / related / none, with reasons; supported, partial, conflicting). It writes nothing in the user's voice.
3. **Response** (`response.compose`): returns the stored FAQ answer, assembles an answer from the evidence with references, answers the supported part and names the unknown part, reports a conflict, or declines. A language model is optional and checked.

The layers only exchange plain data (`tests/test_evidence.py::test_retrieval_evidence_and_response_are_independent_layers`). Before/after numbers, the miss analysis and the limits are in [docs/evidence-results.md](docs/evidence-results.md).

**Which data may influence which decision.** Validation data (generated from the FAQ, or a validation file) chooses models and thresholds. The **deployment regression suite** (`source/regression.csv`, set on the Files tab) is what `ci/improve.py` checks a new version against before promoting it: because it influences a release decision it is not a test set. The **locked benchmark** (the held-out file; for the evidence layer `examples/evidence/locked.csv`, whose sha1 is recorded) is used only to report final results and is never read by the promotion gate.

## Evidence-aware answers: a similar question is not an answer

A high similarity does not mean the stored answer contains what was asked ("How much is a bath?" is very similar to "What services do you offer?", whose answer lists a bath and no price). `Engine.respond(...)` / `POST /respond` / the Test tab keep four signals apart: **retrieval similarity** (cosine, not a probability), **answer sufficiency** (the share of the question's parts the FAQ answer explicitly supports), **evidence support** (the share supported by what is finally used) and a **response confidence category** (rule-based, not calibrated). Outcomes: the stored FAQ answer when it supports every part (no model, no fallback); an answer assembled only from document and web passages, with source and location references, when it does not; an honest "this does not answer it" with an offer to clarify or escalate; or a report that sources conflict (different amounts of the same kind, or yes against no) without choosing. A model is optional and is only asked to combine passages already known to support the answer; its output is discarded if it states a number, or leans on words, that are not in the passages. Limits are written in `src/faqengine/runtime/evidence.py` and below.

## Held-out questions (optional, and better than the generated checks)

A CSV with a column of questions (`query`/`question`) and one saying what each should find: `entry_id` (an id, an exact question or heading; several ids separated by `|`) or `expected_behavior: abstain`. Optional: `type`, a topic column. A `split` column is ignored: every row is a test question. Types: `paraphrase`, `answer-terms`, `typo`, `short`, `multi-part` (several entries, all wanted), `multilingual` (another language or Hinglish), `ambiguous` (several entries, any one is right), `out-of-scope-near` (related but unanswered) and `out-of-scope-far` (completely unrelated), or just `out-of-scope`; out-of-scope rows leave `entry_id` empty. `faqengine heldout-convert` checks a file against your knowledge base, removes repeats and writes it in this plain format. Write them without looking at the FAQ's phrasings: any question that is a near-duplicate of a training phrasing is removed and listed in the report, so alternate phrasings cannot end up on both sides.

With a held-out file the report shows Hit@1, P@K, R@K, MRR@10 and nDCG@10 for keyword search, the unchanged base model and the trained model, overall and by type; whether training helped (a paired bootstrap interval, and an improvement is claimed only if it excludes zero); and refusal validated separately (near-topic against unrelated questions, a threshold sweep, AUROC). The fine-tuned model is the default only if it won on the validation questions; the test questions never choose, they only decide whether an improvement may be claimed (the report says when they show none, or a loss). `faqengine auto --kb faq.csv --heldout heldout.csv --out runs/x` does the same from the command line.

## Any field, any language

Nothing in the engine knows what a FAQ is about, and the repository does not test it on one business: `examples/domains/` holds FAQs from unrelated fields (a clinic, software
support, a restaurant, general trivia) and two languages (Spanish, Hinglish), each with typed held-out questions written in other words than the FAQ's own, and
`python scripts/domain_sweep.py --multilingual` runs the whole pipeline on all of them and writes [docs/domain-sweep.md](docs/domain-sweep.md). What it takes to not fall apart:

- **Whatever file arrives.** `.csv`, `.tsv`, `.xlsx` (one sheet per category is fine), `.json`, `.jsonl`; semicolon-separated "CSV" and Windows or UTF-16 encodings; column names in several languages (Pregunta/Respuesta, Frage/Antwort, प्रश्न/उत्तर...); exports from a CMS (fields nested under `attributes`) and schema.org `FAQPage` JSON; HTML tags and entities inside cells; very long cells.
- **The language is noticed.** The default model (`all-MiniLM-L6-v2`) is English-only. The text is checked before training (alphabets, then common words), and when it is not English the first lines of the report, the Files tab and `faqengine check` say so and offer `multilingual-e5-small` (choose it per project on the Files tab, or `--base multilingual-e5-small`). The sweep shows why: on the Spanish FAQ the English model gets 79% of the held-out questions right first and the multilingual one 100%; on Hinglish (Hindi in Latin letters) keyword search is still as good as either model, which is worth knowing before choosing.
- **The checks adapt to the subject.** The generated "not in the FAQ" questions (weather, trivia, recipes...) are left out when a FAQ answers them (a trivia FAQ does), and the refusal line is not placed by a couple of out-of-scope questions: with fewer than 8 of your own it borrows generic ones and stays below the weakest tenth of the right matches.
- **Small data is handled, not hidden.** With too few questions to judge training, the base model is used and the report says why; the trained model is only the default when it won on dev and is not worse on test.

Limits worth knowing: the held-out questions and FAQs in the examples were written by one author, not customers; "about the same subject but not in the FAQ" questions are the weakest spot in every domain, because one score line cannot tell them from answerable ones (see the near-topic refusal column).

## Comparing two versions of a FAQ

```bash
faqengine compare --previous faq.old.csv --improved faq.new.csv --heldout heldout.csv --map map.csv --out runs/compare --seeds 0,1,2
```

Both versions answer the same held-out questions (near-duplicates of either version's phrasings are removed first). `map.csv` (`id,previous`) says which previous entries each improved entry absorbed, so merging entries is credited by information, not by id. The improved version is trained with several seeds, and the report separates what the new data changed (previous base model against improved base model) from what training changed (improved base model against trained). One real FAQ (the owner's, WASHO) is included as a worked example of this comparison in `examples/washo/` (see its `NOTES.md` for the assumptions), with its report in [docs/washo-engine-report.md](docs/washo-engine-report.md); nothing else in the repository, including the tests and the domain sweep, depends on it.

## It keeps learning: while you test, and after you deploy

- **While you test in the studio:** a question you approve, or correct by naming the entry it belongs to, joins what the engine searches straight away (the next similar question can match it; no retraining). It does not count as accuracy when it matches later, because the engine was told the answer. Only questions a person judged are learned: adding the engine's own unchecked answers would teach it its own mistakes. "Train again using my judged questions" then trains the model on them too.
- **After you deploy** (the downloaded package): `POST /feedback` (or `python ci/improve.py add <entry> "<question>"`) teaches the live engine a question, which is searched on the next request and kept across restarts. `python ci/improve.py retrain` rebuilds the model from the package's own `source/` files plus every learned question, scores the candidate against the live version on your held-out questions, and promotes it only if it is not worse (otherwise the live version stays; with no held-out questions it is staged for a person to promote); `rollback` undoes it; a running server loads the new version on its next request. `ci/github-actions.yml` runs the loop on a schedule. Details, limits and the promotion rule are in [src/faqengine/ci/README.md](src/faqengine/ci/README.md), which is also inside every package.

## What the pipeline does

1. **Views.** Each entry is searched through every phrasing of its question and through question plus answer,
   and scores as its best view.
2. **Training.** Contrastive fine-tuning of a small embedding model (default `all-MiniLM-L6-v2`, 22M
   parameters, Apache-2.0; `bge-small`, `e5-small` and `multilingual-e5-small` are available). Hard
   negatives are the model's own confusable neighbours, a batch never contains two examples of one entry, and
   customer-style variants (no punctuation, keywords only, typos) are generated from each question.
3. **Safeguard.** Training stops on dev data. If no checkpoint beats the untuned model on dev, the untuned
   weights are kept and the report says so.
4. **Evaluation.** Untuned model, tuned model, BM25, and each of the two combined with BM25 (the BM25 weight
   is chosen on dev, so the hybrid baseline is not a strawman), reported as Hit@1, P@K, R@K, MRR@10, nDCG@10,
   with bootstrap intervals on the differences, plus training, dev and test loss.
5. **Thresholds.** Two scores split answers into `direct` (return the stored answer, no LLM), `llm` (give the
   matched entries to your own LLM) and `none` (not in the knowledge base). `direct` is the lowest score whose
   precision is certified at 95% with 90% confidence on dev; it can be absent when dev is too small to
   certify anything, and the report says why.
6. **Export.** ONNX model (int8 if it loses no retrieval quality on dev, otherwise fp32), tokenizer,
   embeddings, thresholds, report and a stdlib HTTP server. The engine's agreement with PyTorch is checked, and
   thresholds are recalibrated on the engine that ships.

Training writes `events.jsonl` (stage, step, loss, learning rate, samples per second, memory, ETA), which is
what a live dashboard will read in Phase 1.

## More phrasings with an LLM

The measured gain comes from phrasing diversity, and most businesses write one phrasing per entry, so this
step is what makes training worth running.

```bash
export GEMINI_API_KEY=...        # in your own terminal; the key is never taken from the command line
faqengine paraphrase --check     # confirms the endpoint, model name and key
faqengine paraphrase --kb faq.csv --out faq.expanded.csv --n 5
```

It works with any OpenAI-compatible endpoint (`--base-url`, `--model`, `--key-env`): Gemini by default, or Groq,
OpenRouter, or a local Ollama (`--base-url http://localhost:11434/v1`, no key). Only each FAQ question and its
answer are sent. Results are cached beside `--out`, so a rerun or a crash repeats no call. Train on the expanded
file. Tested against a fake OpenAI-style server (retries, errors, caching); not yet run against a real LLM.

## Benchmark

```bash
faqengine bench --base minilm-l6 --entries 30 --k 1,2,4,8 --seeds 0-2 --out runs/bench/main.jsonl
faqengine bench --base minilm-l6 --entries 30 --k all --seeds 0-1 --out runs/bench/ceiling.jsonl
faqengine bench-summary runs/bench/main.jsonl
```

Banking77 customer-support questions are cast as FAQ retrieval: each of 30 random intents becomes an entry
with `k` real customer phrasings, the model sees nothing else, and test questions come from the official test
split (plus questions about the other 47 intents as out-of-scope traffic). Details in `src/faqengine/bench.py`.

## Optional language model, hosted mode, and what is and is not verified

* **Local-only is complete.** No key, no account, no internet needed to answer ([docs/local-deployment.md](docs/local-deployment.md); proved with every network connection blocked).
* **A language model is optional,** per project/business, and can only reword answers the evidence already supports; every sentence it writes is checked claim by claim against the evidence first, and a failed check falls back to the stored wording and says so ([docs/llm-integration.md](docs/llm-integration.md), measured in [docs/llm-evaluation.md](docs/llm-evaluation.md)).
* **Hosted mode** (Supabase for data, Auth and files; Vercel for the web app; a small always-on Python service for the engine): [docs/supabase-architecture.md](docs/supabase-architecture.md), [docs/vercel-deployment.md](docs/vercel-deployment.md). Row level security, tenant isolation, roles, encrypted keys and the release gate are tested on a real PostgreSQL; **no live Supabase or Vercel deployment has been tested.**
* How retrieval and evidence work and how they were measured: [docs/retrieval-architecture.md](docs/retrieval-architecture.md), [docs/evidence-architecture.md](docs/evidence-architecture.md), [docs/evidence-results.md](docs/evidence-results.md); evaluation rules: [docs/dataset-integrity.md](docs/dataset-integrity.md); risks: [docs/security.md](docs/security.md).
* **Three ways to run a model, one pipeline** (platform-hosted, your own key, your own computer; deterministic by default, every AI mode off until the operator switches it on and the business agrees to a provider whose terms are verified): [docs/architecture.md](docs/architecture.md), [docs/local-companion.md](docs/local-companion.md), [docs/usage-and-billing.md](docs/usage-and-billing.md), [docs/model-selection.md](docs/model-selection.md), [docs/operations.md](docs/operations.md), [docs/performance.md](docs/performance.md). **No real provider has been called and no model benchmark has been run**; the summary of everything is [docs/final-report.md](docs/final-report.md).
* **A deterministic fast path:** a question the engine already answers completely, currently and verifiably (an exact FAQ entry, a calculation worked out in code) is answered without asking any model; anything less goes on through the pipeline as before. Conditions, measurements against the deterministic baseline and honest limits: [docs/fast-path.md](docs/fast-path.md).
* **Read [docs/production-readiness.md](docs/production-readiness.md) before relying on any of it:** it lists what is completed, what is only partly tested, what is not built and what is blocked on credentials.

## Tests

```bash
cd web && npm install && npm test       # the web API and the page's static checks (no Supabase, no Vercel needed)
```

```bash
pytest                                  # fast, no model needed (the Postgres tests skip themselves when no local PostgreSQL with pgvector is available)
FAQENGINE_SLOW=1 pytest                 # also trains, exports and checks the package, the website flows and the retrain-and-promote loop (about 20 minutes)
```

## Not built yet

- The Vaizent-style extras around the studio: agent wizard and templates, embeddable widget, leads, analytics, API keys (Phase 2 onwards); the studio itself is the part that exists.
- Proof that LLM-written phrasings give the same gain as real ones: `faqengine bench-paraphrases` then
  `faqengine bench --k 1 --paraphrases data/paraphrases.json` measures it; it needs an API key, so it has not been run.
- Chunking of long answers: only the first ~100 words of an answer are embedded in the combined view.
- Web pages that need JavaScript or a login (the reader runs no scripts), and PDFs behind a link (upload those as files); multilingual and Hinglish evaluation (the multilingual base is available, untested).
- A Dockerfile for the package (it needs a container to be tested; none was available here).
