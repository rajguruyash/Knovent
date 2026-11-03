# Local deployment (local-only mode)

Everything in this document runs on one computer. **It needs no Supabase, no Vercel, no language-model key and no internet connection to answer questions.** That is checked, not assumed: `tests/test_offline.py` forbids every network connection and then runs the whole answering workflow with the real embedding model on a real exported package (retrieval, evidence, response, abstention, clarification, learning from feedback).

## The pieces

| | What it is | Run |
|---|---|---|
| **Studio** | the local website: upload FAQs, documents and web pages; train; test; judge answers; export | `faqengine studio` then open http://127.0.0.1:8765 (`--data` to choose where projects are kept) |
| **Engine package** | a folder or zip you download from Studio: ONNX model, tokenizer, your knowledge and its vectors, thresholds and a small runtime (numpy, onnxruntime, tokenizers only) | `python runtime/engine.py . "your question"` or `python runtime/serve.py --package .` |
| **Command line** | `faqengine train / compare / export / …` (see `docs/REFERENCE.md`) | |

Setting up: `python -m venv .venv && .venv/bin/pip install -e ".[dev]"`. Training uses PyTorch (Apple GPU, CUDA or CPU); **answering from an exported package does not**.

## What local mode does, with no model and no network

1. **Ingest:** every kind of source goes through the same pipeline (identify, parse, normalise, chunk, keep source, heading and address, embed, one index). CSV, TSV, Excel (every sheet), JSON/JSONL (nested objects too) tables with **any header names** (FAQ tables, prose tables, or tables of values read row by row; the Studio's *Columns* control overrides the automatic guess); PDF, Word, text, Markdown and HTML documents (cut into passages, embedded and searched like the FAQ, and used as evidence in answers); web pages and same-site crawls (Studio, internet needed only for the crawl itself, robots.txt obeyed, private addresses refused).
2. **Retrieval** (`runtime/retrieval.py`): MiniLM embeddings and BM25 keywords blended (keyword share 0.3) in **one ranking for every kind of source**, with the model's best four matches always kept in the pool the evidence engine reads; what is expired, not yet valid or replaced is left out and, if only it answers, reported as out of date (`docs/unified-knowledge.md`).
3. **Evidence** (`runtime/evidence.py`): which parts of the question the candidates actually support, and how.
4. **Response** (`runtime/response.py`): the stored FAQ answer, an answer assembled from passages with references, an answer to part of the question with the rest named as unknown, a report that sources disagree, a question back, or an honest "this does not answer it".
5. **Feedback:** approved or corrected questions are searched at once as extra ways of asking an entry and used at the next retraining.

## The language model is optional, per project

On the first screen of a project Studio asks "Would you like to enable LLM integration for enhanced AI-generated answers?" Answering **No** is a complete setup. Answering **Yes** asks for a provider, key and model, checks them, and can be changed, turned off or removed later (details: `docs/llm-integration.md`).

* The key is stored in `studio_data/projects/<id>/llm.json`, readable by the owner of the files only (mode 0600). It is **not encrypted** in local mode; the page says so. Anyone who can read your disk can read it.
* It is never returned by the Studio's API, never written to a log, and never put in an exported package (the exporter does not read it, and `export.assert_no_secrets` refuses to finish a package that contains anything credential-shaped).
* A package's own server (`runtime/serve.py`) answers without a model. To use one from your own code: `Engine(path).respond(question, llm=LLM(LLMConfig(...)))` with `faqengine.runtime.llm`.

## Changing the files later: versions, and updating the index without training

* **Adding a file with the same name replaces it** (never a duplicate). Each file keeps a **version** and the Files table shows what the last replacement changed (`v2 · +1 −1 =3`: entries or passages gained, lost, kept). The same bytes again are not a new version. A file that cannot be read is refused and the old one is put back.
* After a change the page says the files are newer than the model. **Update the search index** (seconds) searches the current files with the model as it is: the vectors of text that did not change are kept, only new or changed text is embedded, and a failure leaves the index as it was. **Train again** (minutes) also lets the model learn new wording from a FAQ. Until one of them is done, search and tests use the old files. After an index update the page says the model itself was trained on earlier files.
* **Removing a file** removes its passages from the index at the next update, and a web page added again is read afresh. A document can state when it stops being valid, starts, or what it replaces, in a header (`docs/unified-knowledge.md`).

## Moving a package, keeping it current

Copy the folder anywhere with Python 3.10+ and `pip install -r runtime/requirements.txt`. `ci/improve.py` retrains a package from its own sources plus what the deployment learned, checks the candidate against the deployment regression suite, and swaps it in only if it passes (rollback: keep the previous version folder; see `docs/REFERENCE.md`).

## What local mode does not do

No users, roles or tenants (one person at a keyboard; each project is its own business), no hosted dashboard, no shared knowledge across computers, no encrypted storage of the key, no scheduled re-crawling.
