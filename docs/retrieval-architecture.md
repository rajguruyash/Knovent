# Retrieval architecture

Retrieval finds the entries that *might* contain the answer. It says how it found them. It does not decide whether they answer the question (that is `docs/evidence-architecture.md`) and it writes nothing.

## The pipeline (`src/faqengine/runtime/retrieval.py`)

1. **Normalise the question** (`textutil.normalize_query`): Unicode composed (NFKC), control and zero-width characters removed, whitespace collapsed, length capped at 2,000 characters. Case is kept for the model; the keyword channel lower-cases for itself.
2. **Embed** the question (the caller owns the model: the local engine's ONNX model, or the hosted service's shared MiniLM).
3. **Two scores per entry.** An entry is searched through several *views* (its question, alternative phrasings, question plus answer); each channel scores an entry as its best view.
   * `similarity_score`: cosine of the question and the view, from all-MiniLM-L6-v2 (or the project's fine-tuned model).
   * `keyword_score`: BM25 (k1 1.5, b 0.75) over the views' words, with the same light stemming and stop-word list the evidence engine uses; 0 when no word is shared.
4. **Hybrid blend.** `combined_score = (1 - α)·unit(similarity) + α·unit(keyword)`, each stretched to 0..1 over this question's entries, α = **0.3**. With no word in common the keyword part is 0 and the order is the model's (the candidate says so in `retrieval_method`).
5. **Rank every kind of entry together.** A FAQ entry, a document passage, a spreadsheet row, a JSON record and a web page compete in **one** ranking by `combined_score` and nothing else: **no kind gets a place of its own or a head start** (tested both ways round). Ties go to the entry that comes first, so results are reproducible. The top **10** (`CANDIDATES`) are returned; the evidence engine examines the first **8** (`EVIDENCE_POOL`).
6. **Freshness.** An entry that is not current today (its source has expired, has not started, or is replaced by a newer source; `docs/unified-knowledge.md`) is **never a candidate**. The best three such matches are named in `diagnostics.inactive_matches`, so that a question only they answer is told *why* it has no answer instead of being answered from out-of-date material. Then the **guard** (below), **deduplication** (the same text under another id: the better-ranked one stands, the other id is listed in `duplicates`) and, optionally, a **source filter** (`allowed_sources`).
7. **Return structured candidates** and **diagnostics**.

Each candidate carries: `entry_id`, `kind` (faq / passage), `question`, `answer`, `source`, `category`, `location` (source, section, entry), `similarity_score`, `keyword_score`, `combined_score`, `retrieval_method`, the `matched` view and its kind, `duplicates`, `rank`. `score` is kept as the cosine because the evidence engine reads it as a *similarity*; it is documented, in the output, as **not a probability and not an answer confidence**. Diagnostics: method, α, entries and views searched, how many entries had a keyword match, duplicates dropped, source filter, the kinds among the candidates, `inactive_matches`, `unknown_names` (capitalised names in the question that appear nowhere in the knowledge: the evidence engine will not let a similar sentence stand in for them), search and embedding milliseconds. The Test screen shows all of it.

Retrieval scores are never presented as confidence. The evidence layer's own signals (`answer_sufficiency`, `evidence_support`) and a rule-based category (`response_confidence`, "not a calibrated probability") are separate fields.

## The cross-encoder decision

A cross-encoder reranker was tested against this retrieval and **is not used by default**. The experiment (`docs/cross-encoder-experiment.md`, preserved unchanged) ran five configurations on the same 599 test questions: reranking the model's top 20 lowered right-first accuracy from 78.1% to 69.4%, and reranking the hybrid's from 79.5% to 69.3%, with 98 questions broken for 46 fixed. Latency and memory cost were also large (about 457 ms per question on CPU). The recommendation there stands: do not integrate; experiment further only via score fusion, a margin trigger, or a reranker trained on FAQ data. Nothing in `src/` imports the experiment (tested).

## Measured: hybrid against the model alone

Nine datasets (`experiments/cross_encoder/datasets.py`: held-out questions of Pawfect, six domains and a bike shop, plus Banking77 with 77 intents), the pretrained MiniLM, the **runtime's own** BM25 and blend (`scripts/retrieval_check.py`). α was chosen on the development questions only (MRR@10 by α — 0.0: 0.832 · 0.1: 0.843 · 0.2: 0.848 · 0.3: 0.851 · 0.5: 0.837 · 0.7: 0.821 · 1.0: 0.752) and the numbers below are the **test** questions.

| Configuration | Right first (macro) | MRR@10 (macro) | Recall@5 (macro) | Right first (all 599 questions pooled) |
|---|---|---|---|---|
| Model alone (MiniLM embeddings) | 80.1% | 0.866 | 94.9% | 78.1% |
| Keywords alone (BM25) | 72.0% | 0.786 | 85.1% | 59.3% |
| Hybrid (blend, keyword share 0.3) | 85.1% | 0.895 | 92.6% | 78.6% |
| **Hybrid + guard (what runs)** | 85.1% | 0.895 | 92.6% | 78.6% |

Macro = the mean over the nine datasets (so Banking77's 462 questions do not dominate); the pooled column weights every question equally and is dominated by Banking77, where the hybrid is 1.5 points *worse* than the model alone (77.7% → 76.2%).

| Dataset | Test questions | Model alone | Keywords alone | Hybrid | Hybrid + guard |
|---|---|---|---|---|---|
| pawfect | 38 | 68.4% | 81.6% | 84.2% | 84.2% |
| clinic | 13 | 84.6% | 61.5% | 84.6% | 84.6% |
| hinglish-store | 11 | 54.5% | 72.7% | 63.6% | 63.6% |
| restaurant | 11 | 100.0% | 72.7% | 100.0% | 100.0% |
| saas | 11 | 90.9% | 63.6% | 90.9% | 90.9% |
| spanish-hotel | 11 | 54.5% | 72.7% | 72.7% | 72.7% |
| trivia | 11 | 100.0% | 90.9% | 100.0% | 100.0% |
| bike-shop | 31 | 90.3% | 77.4% | 93.5% | 93.5% |
| banking77 | 462 | 77.7% | 54.3% | 76.2% | 76.2% |

Differences are a handful of questions per dataset (six datasets have 11 test questions: one question is 9 points). Read it as direction, not precision. The runtime's BM25 (its own tokenizer) lands within about a point of the experiment's reference hybrid (macro right-first 85.1% against 85.5%).

### Why a guard, and how its size was chosen

The evidence engine reads only the first few candidates (**eight** since the ranking became unified; the numbers below were measured when it read five FAQ candidates, and *g = 4* was kept). A blend that raises the right entry more often than it lowers it still pushed a candidate out of those five now and then (macro Recall@5 fell 94.9% → 92.6% on the test questions). `guard_order` keeps the model's own *g* best matches inside that pool (the order inside stays the blend's). *g* was chosen on the development questions, not the test questions: the best on all three development measures was **g = 4**.

| Guard | Dev: right first | Dev: MRR@10 | Dev: Recall@5 | Test: right first | Test: MRR@10 | Test: Recall@5 |
|---|---|---|---|---|---|---|
| model alone | 81.0% | 0.874 | 94.9% | 80.1% | 0.866 | 94.9% |
| 0 | 84.4% | 0.894 | 95.9% | 85.1% | 0.895 | 92.6% |
| 1 | 84.4% | 0.894 | 95.9% | 85.1% | 0.895 | 92.6% |
| 2 | 84.4% | 0.894 | 95.8% | 85.1% | 0.895 | 92.6% |
| 3 | 84.4% | 0.894 | 95.9% | 85.1% | 0.895 | 92.5% |
| 4 **(chosen)** | 84.4% | 0.895 | 96.7% | 85.1% | 0.896 | 93.9% |
| 5 | 84.3% | 0.891 | 94.9% | 85.1% | 0.897 | 94.9% |

With the guard at 4, right-first and MRR keep the hybrid's gain and Recall@5 is within one point of the model alone on the test questions (93.9% against 94.9%). The remaining gap is three questions in three small datasets.

## What is not measured or not built

* **Search latency grows with the size of the knowledge.** Measured on this machine (`scripts/operational_metrics.py`, a deliberately repetitive synthetic corpus of 30 distinct words, which is the worst case for keyword search): median 0.8 ms at 1,000 entries, 8.6 ms at 5,000, 72 ms at 20,000, plus 0.8 ms to embed the question. Real text has a larger vocabulary and shorter keyword lists, so these are upper-end figures, but a business with tens of thousands of entries should expect tens of milliseconds, and 100,000 entries was not measured. The keyword channel is pure Python; an inverted index in a faster language, or `match_embeddings` for the semantic channel, would be the next step.
* Keyword search uses an English stop-word list and stemmer; for other languages it still helps (words are matched as written), but it is weaker. The Spanish and Hinglish sets above are where it helped most and where Recall@5 moved most.
* Query expansion, spelling correction and synonyms are not implemented; the language model's retrieval assistance (`docs/llm-integration.md`) is the optional substitute.
* The pgvector index and `match_embeddings` exist in the schema; the service does not use them (`docs/supabase-architecture.md`).

## One ranking for every kind of source (what changed, and what it was measured against)

Before, FAQ entries and passages were ranked in two lists and the evidence engine looked at five FAQ entries and a few passages. A business with a FAQ, a handbook, a price list and a web page needs them to compete on equal terms: the list a source ends up in says nothing about whether it answers. Now there is one list (above). What it was measured against is in `docs/unified-knowledge.md`: the same questions through the old retrieval and the new one, and the old evidence benchmark, which must come out unchanged on its development and regression sets (it does).

Passages from structured sources are written so that **each sentence can stand as evidence on its own** ("Pro plan, seats: 5." rather than a bare "5"), because ranking works on views and evidence on sentences. A row of a file also carries what the file says its rows are ("Pune city (offices), address: …") unless the row's own name already says it.
