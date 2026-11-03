# Cross-encoder reranking: an isolated experiment

**Question.** Does a lightweight pretrained cross-encoder, reading a question together with a candidate FAQ entry, rank the right entry higher than the engine's existing retrieval? **Scope.** Experiment only: nothing in `src/` imports it, the evidence and response engines, the embedding model's weights and the abstention rules are untouched, no cross-encoder was fine-tuned, and nothing is committed. Code: `experiments/cross_encoder/`; raw results: `runs/cross-encoder/` (per-question candidate records with original rank, original similarity, reranker score and final rank in `records.jsonl`).

## Verdict

**No, not as configured: it does not beat the best existing configuration, and it is not reliably better than the weaker one.** Pooled over the 599 test questions, right-first accuracy was 78.1% for the existing embedding retrieval, 79.5% for the existing hybrid, and 69.4% / 69.3% with the cross-encoder on top of them (MRR@10 0.851 / 0.858 → 0.784 / 0.782). The pooled figure is dominated by Banking77 (462 questions), where it is clearly worse; counting each of the nine datasets once (macro average) right-first goes 80.1% → 83.3% for embedding + cross-encoder, which looks like a gain over the embedding alone, but the existing hybrid is already 85.5% and hybrid + cross-encoder is 83.3%. So the cross-encoder at best repairs some of what the keyword blend repairs already, on small datasets of 11–38 questions where one question is 3–9 points, and it breaks more questions than it rescues overall. Recall@5, the number that matters for the evidence engine, was 93.4% (embedding), 93.2% (hybrid) and 89.4% / 88.7% with the cross-encoder. **Recommendation: do not integrate this configuration; experiment further only along the lines in the last section.**

## 1. Where a cross-encoder fits in the current architecture

The product has three layers (README): retrieval → evidence → response. Retrieval is `Engine.candidates(query, n)` / `ProjectEngine.candidates`: it ranks every entry by embedding similarity (an entry scores as its best view; a keyword and a hybrid score exist in the evaluation code, `bm25.py` and `heldout_report.hybrid_scores`, but the engine ranks by embedding alone) and returns the best `n` FAQ entries and best `n` passages. `evidence.assess` then judges up to **5 FAQ candidates and all the passages** it is given, part by part; it uses rank only as a tie-break (`closeness`).

A reranker belongs **between `candidates()` and `assess()`**: reorder (and, if wanted, widen) the candidate lists, and hand the same plain hit dicts on. That changes no existing behaviour while it is off, needs no change to the evidence or response layers, and keeps the reranker's score out of the confidence signals (the experiment records it only beside the original similarity). Consequence for what matters: because the evidence engine inspects the top 5 FAQ entries rather than trusting rank 1, **Recall@5 is the retrieval number that bears on answer quality; right-first matters less.**

## 2. Model selection

Requirements: pretrained and not fine-tuned, CPU-capable, Apple Silicon-capable, offline after download, small. Four lightweight cross-encoders were downloaded and measured; two larger ones were considered by size only.

| Model | Parameters | Download (weights) | Licence | Dev MRR@10: embedding → +cross-encoder | Notes |
|---|---|---|---|---|---|
| `cross-encoder/ms-marco-TinyBERT-L-2-v2` | 4.4 M | 16.7 MB | Apache-2.0 | 0.832 → 0.748 | smallest; MS MARCO trained |
| `cross-encoder/ms-marco-MiniLM-L-6-v2` | 22.7 M | 86.7 MB | Apache-2.0 | 0.832 → 0.808 | MS MARCO trained, the usual lightweight reranker **(chosen)** |
| `cross-encoder/ms-marco-MiniLM-L-12-v2` | 33.4 M | 127.3 MB | Apache-2.0 | 0.832 → 0.808 | twice the depth of L6 |
| `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | 117.6 M | 448.8 MB | Apache-2.0 | 0.832 → 0.741 | multilingual (mMARCO), 118M parameters |
| `BAAI/bge-reranker-base` | 278 M | 1060.7 MB | MIT | not run | rejected on size and latency before measuring |
| `BAAI/bge-reranker-v2-m3` | 568 M | 2165.9 MB | Apache-2.0 | not run | rejected on size (more than the whole current package) |

**Selection rule, fixed before looking at test results:** the highest dev-pool MRR@10 of embedding + cross-encoder; within 0.005, the smaller model. It selected `cross-encoder/ms-marco-MiniLM-L-6-v2`. That is not an endorsement: on the dev pool **all four** models ranked worse than the embedding retrieval alone (dev MRR@10 0.832 for the embedding). MiniLM-L6 was the least harmful, is 87 MB, and its larger sibling L12 did no better.

## 3. The experiment

For every question: the existing retrieval ranks all entries; the **top 20** are passed to the cross-encoder as (question, candidate) pairs, where a candidate is `question. answer` for a FAQ entry and `heading. text` for a passage (truncated at 256 tokens); the 20 are reordered by the cross-encoder's score and everything below keeps its retrieval order. Recorded per candidate: original rank, original similarity, reranker score, final rank. The reranker's score is used only to order the 20 candidates of one question; it is not an answer confidence and is never compared across questions.

- **Embedding**: the pretrained `all-MiniLM-L6-v2` (the engine's base model, un-tuned, so that nothing is trained on the datasets being measured).
- **Keyword**: the engine's BM25 over each entry's phrasings.
- **Hybrid**: `hybrid_scores(embedding, keyword, α)` with α = 0.3 chosen on the dev pool by MRR@10 (the same function as the held-out hybrid experiment).
- **Questions**: identical for all five configurations. Pools: *dev* chooses α and the model; *test* is reported and chooses nothing; *locked* is the locked evidence benchmark (`examples/evidence/locked.csv`), run once for the chosen model after everything else was fixed.

| Dataset | Entries | Questions | Source |
|---|---|---|---|
| pawfect | 17 | 90 | Pawfect held-out (diagnostic set; split by a hash of the question) |
| clinic | 12 | 27 | domain example held-out (its own dev/test split) |
| hinglish-store | 12 | 24 | domain example held-out (its own dev/test split) |
| restaurant | 12 | 24 | domain example held-out (its own dev/test split) |
| saas | 12 | 24 | domain example held-out (its own dev/test split) |
| spanish-hotel | 12 | 24 | domain example held-out (its own dev/test split) |
| trivia | 12 | 24 | domain example held-out (its own dev/test split) |
| bike-shop | 10 | 41 | bike-shop example held-out (its own dev/test split) |
| banking77 | 77 | 693 | Banking77: 77 real intents × 4 real phrasings; real customer utterances (dev 3/intent, test 6/intent) |
| evidence | 19 | 36 | evidence sets: dev+regression rows → dev, locked rows → locked |

Caveat that shapes everything below: **all but Banking77 are tiny knowledge bases (10–19 entries), so the top 20 is the whole knowledge base** and Recall@20 is 100% by construction there; only Banking77 (77 entries, many near-identical intents) exercises candidate recall and hard negatives. The small sets were written by one author; Banking77 is real customer language.

## 4. Results: the five configurations on the pooled test questions

Micro average over all test questions (Banking77 contributes 462 of the 599):

| Configuration | n | Right first | R@5 | R@10 | R@20 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|---|
| 1. Keyword only | 599 | 60.6% | 84.7% | 91.8% | 96.0% | 0.709 | 0.756 |
| 2. Embedding only (existing) | 599 | 78.1% | 93.4% | 96.9% | 99.2% | 0.851 | 0.877 |
| 3. Hybrid (existing) | 599 | 79.5% | 93.2% | 97.2% | 99.0% | 0.858 | 0.883 |
| 4. Embedding + cross-encoder | 599 | 69.4% | 89.4% | 95.8% | 99.2% | 0.784 | 0.824 |
| 5. Hybrid + cross-encoder | 599 | 69.3% | 88.7% | 95.7% | 99.0% | 0.782 | 0.822 |

Macro average (each of the 9 test datasets counts once):

| Configuration | n | Right first | R@5 | R@10 | R@20 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|---|
| 1. Keyword only | 9 | 74.3% | 85.3% | 97.5% | 99.4% | 0.797 | 0.833 |
| 2. Embedding only (existing) | 9 | 80.1% | 94.9% | 97.9% | 99.9% | 0.866 | 0.889 |
| 3. Hybrid (existing) | 9 | 85.5% | 92.9% | 99.3% | 99.9% | 0.896 | 0.915 |
| 4. Embedding + cross-encoder | 9 | 83.3% | 90.3% | 97.2% | 99.9% | 0.873 | 0.892 |
| 5. Hybrid + cross-encoder | 9 | 83.3% | 90.2% | 97.1% | 99.9% | 0.873 | 0.891 |

Without Banking77 (the 8 small datasets pooled, 137 questions):

| Configuration | n | Right first | R@5 | R@10 | R@20 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|---|
| 1. Keyword only | 137 | 79.6% | 86.5% | 97.8% | 100.0% | 0.840 | 0.859 |
| 2. Embedding only (existing) | 137 | 79.6% | 94.5% | 97.4% | 100.0% | 0.866 | 0.883 |
| 3. Hybrid (existing) | 137 | 87.6% | 93.1% | 99.3% | 100.0% | 0.912 | 0.920 |
| 4. Embedding + cross-encoder | 137 | 86.1% | 91.6% | 97.8% | 100.0% | 0.895 | 0.903 |
| 5. Hybrid + cross-encoder | 137 | 86.1% | 91.6% | 97.8% | 100.0% | 0.895 | 0.903 |

Locked benchmark (22 answerable questions, run once, chosen model only; one question is 5 points):

| Configuration | n | Right first | R@5 | R@10 | R@20 | MRR@10 | nDCG@10 |
|---|---|---|---|---|---|---|---|
| 1. Keyword only | 22 | 72.7% | 95.5% | 100.0% | 100.0% | 0.833 | 0.875 |
| 2. Embedding only (existing) | 22 | 81.8% | 100.0% | 100.0% | 100.0% | 0.890 | 0.918 |
| 3. Hybrid (existing) | 22 | 86.4% | 100.0% | 100.0% | 100.0% | 0.924 | 0.944 |
| 4. Embedding + cross-encoder | 22 | 86.4% | 95.5% | 95.5% | 100.0% | 0.909 | 0.921 |
| 5. Hybrid + cross-encoder | 22 | 86.4% | 95.5% | 95.5% | 100.0% | 0.909 | 0.921 |

Hybrid + cross-encoder is almost identical to embedding + cross-encoder everywhere: with 20 candidates the cross-encoder's order dominates whatever produced the 20.

## 5. Per dataset (test pool): right first / MRR@10

| Dataset | n | Keyword only | Embedding only (existing) | Hybrid (existing) | Embedding + cross-encoder | Hybrid + cross-encoder |
|---|---|---|---|---|---|---|
| pawfect | 38 | 86.8% / 0.90 | 68.4% / 0.80 | 86.8% / 0.91 | 84.2% / 0.88 | 84.2% / 0.88 |
| clinic | 13 | 61.5% / 0.68 | 84.6% / 0.92 | 84.6% / 0.91 | 92.3% / 0.96 | 92.3% / 0.96 |
| hinglish-store | 11 | 72.7% / 0.77 | 54.5% / 0.66 | 63.6% / 0.73 | 72.7% / 0.80 | 72.7% / 0.80 |
| restaurant | 11 | 72.7% / 0.77 | 100.0% / 1.00 | 100.0% / 1.00 | 72.7% / 0.75 | 72.7% / 0.75 |
| saas | 11 | 63.6% / 0.74 | 90.9% / 0.95 | 90.9% / 0.95 | 100.0% / 1.00 | 100.0% / 1.00 |
| spanish-hotel | 11 | 81.8% / 0.84 | 54.5% / 0.66 | 72.7% / 0.76 | 72.7% / 0.78 | 72.7% / 0.78 |
| trivia | 11 | 90.9% / 0.92 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 |
| bike-shop | 31 | 83.9% / 0.90 | 90.3% / 0.95 | 93.5% / 0.95 | 90.3% / 0.94 | 90.3% / 0.94 |
| banking77 | 462 | 55.0% / 0.67 | 77.7% / 0.85 | 77.1% / 0.84 | 64.5% / 0.75 | 64.3% / 0.75 |

## 6. Per category (test pool): right first / MRR@10

Categories are the kinds of question the example files label (paraphrase, short, typo, answer-terms, ambiguous = several acceptable entries), plus Banking77's real customer utterances. *Paraphrase* is the paraphrase-performance measure asked for.

| Category | n | Keyword only | Embedding only (existing) | Hybrid (existing) | Embedding + cross-encoder | Hybrid + cross-encoder |
|---|---|---|---|---|---|---|
| ambiguous | 14 | 92.9% / 0.94 | 71.4% / 0.82 | 85.7% / 0.88 | 100.0% / 1.00 | 100.0% / 1.00 |
| answer-terms | 21 | 95.2% / 0.98 | 90.5% / 0.95 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 |
| customer utterance | 462 | 55.0% / 0.67 | 77.7% / 0.85 | 77.1% / 0.84 | 64.5% / 0.75 | 64.3% / 0.75 |
| paraphrase | 80 | 72.5% / 0.78 | 75.0% / 0.83 | 83.8% / 0.89 | 78.8% / 0.84 | 78.8% / 0.84 |
| short | 17 | 94.1% / 0.95 | 94.1% / 0.96 | 94.1% / 0.95 | 94.1% / 0.95 | 94.1% / 0.95 |
| typo | 5 | 40.0% / 0.54 | 80.0% / 0.87 | 80.0% / 0.87 | 80.0% / 0.87 | 80.0% / 0.87 |

Locked benchmark by category:

| Category | n | Keyword only | Embedding only (existing) | Hybrid (existing) | Embedding + cross-encoder | Hybrid + cross-encoder |
|---|---|---|---|---|---|---|
| direct | 6 | 100.0% / 1.00 | 83.3% / 0.92 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 |
| multi-part | 2 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 | 100.0% / 1.00 |
| paraphrase | 9 | 55.6% / 0.72 | 77.8% / 0.84 | 66.7% / 0.81 | 88.9% / 0.89 | 88.9% / 0.89 |
| partial | 5 | 60.0% / 0.77 | 80.0% / 0.90 | 100.0% / 1.00 | 60.0% / 0.80 | 60.0% / 0.80 |

## 7. Hard negatives

Two views of hard negatives. First, by construction: the right entry has a near-identical neighbour (the nearest other entry's centre has cosine ≥ 0.70 with it). Second, by behaviour: the baseline ranked a wrong entry above the right one: that is the rescued/broken count in section 8. Test questions whose right entry has such a neighbour: 348; the rest: 251.

| Test questions | n | Keyword only | Embedding only (existing) | Hybrid (existing) | Embedding + cross-encoder | Hybrid + cross-encoder |
|---|---|---|---|---|---|---|
| right entry has a near-identical neighbour | 348 | 54.6% / 0.67 | 73.6% / 0.82 | 73.9% / 0.82 | 64.4% / 0.76 | 64.4% / 0.76 |
| no near-identical neighbour | 251 | 68.9% / 0.76 | 84.5% / 0.89 | 87.3% / 0.91 | 76.5% / 0.82 | 76.1% / 0.82 |

## 8. Was the right entry retrieved at all, or retrieved and ranked wrongly?

Test questions, by what the retrieval did with the right entry:

| Configuration | Right entry first | In the top 20, not first | Not in the top 20 |
|---|---|---|---|
| 1. Keyword only | 363 (60.6%) | 212 (35.4%) | 24 (4.0%) |
| 2. Embedding only (existing) | 468 (78.1%) | 126 (21.0%) | 5 (0.8%) |
| 3. Hybrid (existing) | 476 (79.5%) | 117 (19.5%) | 6 (1.0%) |
| 4. Embedding + cross-encoder | 416 (69.4%) | 178 (29.7%) | 5 (0.8%) |
| 5. Hybrid + cross-encoder | 415 (69.3%) | 178 (29.7%) | 6 (1.0%) |

So for these questions the problem is **ranking, not retrieval**: the right entry is almost always among the 20 (the few outside are on Banking77); the open question is whether a reranker orders them better. With the cross-encoder on top of the embedding retrieval: **46 questions rescued** (the right entry was not first and became first), **98 broken** (it was first and stopped being), 74 moved up, 133 moved down, of 599.

## 9. Failure analysis

**Rescued by the cross-encoder (examples from the smaller datasets first)**

- [bike-shop] “do you drop bikes off at home within ten miles” — right entry: “Do you deliver?” (embedding rank 2, reranker score -5.19, final rank 1); reranker's first: “Do you deliver?” (embedding rank 2, reranker score -5.19).
- [clinic] “set up a first visit as a new patient” — right entry: “How do I book an appointment?” (embedding rank 2, reranker score -4.02, final rank 1); reranker's first: “How do I book an appointment?” (embedding rank 2, reranker score -4.02).
- [hinglish-store] “ghar tak saman mangwa sakte hain kya” — right entry: “Kya aap ghar par delivery karte hain?” (embedding rank 5, reranker score 2.44, final rank 1); reranker's first: “Kya aap ghar par delivery karte hain?” (embedding rank 5, reranker score 2.44).
- [hinglish-store] “kharab saman lauta sakte hain” — right entry: “Saman wapas kaise karein?” (embedding rank 2, reranker score 2.81, final rank 1); reranker's first: “Saman wapas kaise karein?” (embedding rank 2, reranker score 2.81).
- [pawfect] “Can you brush my dog's coat at the studio?” — right entry: “What services does Pawfect Grooming Studio offer?” (embedding rank 5, reranker score 4.06, final rank 1); reranker's first: “What services does Pawfect Grooming Studio offer?” (embedding rank 5, reranker score 4.06).

**Broken by the cross-encoder**

- [bike-shop] “i want my money back for a helmet” — right entry: “How do I return an item?” (embedding rank 1, reranker score -11.26, final rank 2); reranker's first: “Can I pay for a bike in instalments?” (embedding rank 2, reranker score -10.04).
- [pawfect] “How can I reserve a slot?” — right entry: “How do I book an appointment?” (embedding rank 1, reranker score -11.45, final rank 13); reranker's first: “Opening schedule” (embedding rank 5, reranker score -11.07).
- [pawfect] “Is advance appointment booking compulsory?” — right entry: “How do I book an appointment?” (embedding rank 1, reranker score -9.49, final rank 2); reranker's first: “Do I need an appointment?” (embedding rank 2, reranker score -6.94).
- [pawfect] “Can I move my slot to another day?” — right entry: “Can I cancel or reschedule?” (embedding rank 1, reranker score -11.06, final rank 2); reranker's first: “Opening schedule” (embedding rank 2, reranker score -10.26).
- [restaurant] “can i get a table for friday” — right entry: “How do I make a reservation?” (embedding rank 1, reranker score -11.31, final rank 6); reranker's first: “Can I book the restaurant for a private event?” (embedding rank 2, reranker score -10.08).
- [restaurant] “i have coeliac disease” — right entry: “Do you offer gluten-free dishes?” (embedding rank 1, reranker score -11.38, final rank 12); reranker's first: “What are your opening hours?” (embedding rank 9, reranker score -11.20).
- [restaurant] “table for two tonight” — right entry: “How do I make a reservation?” (embedding rank 1, reranker score -11.42, final rank 10); reranker's first: “What are your opening hours?” (embedding rank 2, reranker score -10.16).
- [banking77] “When will the transfer be completed?” — right entry: “I didn't think my country-to-country money transfer would take so long. It still says that it's pending so I re-checked all of the account information--everything seems right, so what's the deal?” (embedding rank 1, reranker score -5.35, final rank 3); reranker's first: “How long will my most recent transfer take to go through? The recipient needs the money urgently.” (embedding rank 2, reranker score -2.86).

What the examples show. The ms-marco cross-encoders were trained to judge whether a passage answers a web search query. Used on a FAQ they (a) reward a candidate whose *answer text* mentions the question's words, even when the entry is about something else, and (b) do not know that two entries are siblings (card arrival vs card delivery estimate), which is exactly where this retrieval problem is hard. In Banking77 the candidate's answer is the generic sentence "This answers questions about <intent>", so the reranker has only the question phrasing to go on, and it sees one of an entry's four phrasings where the embedding retrieval uses all four. Where entries have real, specific answers (the Pawfect, clinic, SaaS sets) it does better, and it was often better on the multi-acceptable (ambiguous) questions.

### One follow-up question: does it just see too little?

Variant (test pool only; not pre-declared; reported next to the main result, not chosen from it): score every phrasing of each candidate entry and let the entry take its best score. Pooled test right-first 77.0%, MRR@10 0.846, against 69.4% / 0.784 for the single-phrasing reranker and 78.1% / 0.851 for the embedding retrieval. By dataset (right first): banking77 74.2%, bike-shop 90.3%, clinic 92.3%, hinglish-store 72.7%, pawfect 84.2%, restaurant 81.8%, saas 100.0%, spanish-hotel 63.6%, trivia 100.0%.

## 10. Performance, measured on this machine (Apple M2, 8 GB, offline)

Each row is a separate process with the network switched off (`HF_HUB_OFFLINE=1`, files loaded with `local_files_only`); peak memory is the process's peak resident set (`ru_maxrss`) after the embedding model and again after the reranker was loaded and had scored 60 questions; latency is the median (p95) over 60 questions × 20 candidates in one batch, and one pair at a time; end-to-end is: embed the question (CPU), score every entry, take the top 20, then (for the second figure) rerank them on the device in the row. The machine is a fanless M2 that throttles under sustained load and the MPS rows are noisy (note the p95s); a first run that overlapped another job was discarded and this one ran with nothing else active.

| Model | Params | Weights on disk | Device | Load | 20 candidates, median (p95) | One pair, median | Extra peak memory | End-to-end without → with reranker (median) |
|---|---|---|---|---|---|---|---|---|
| tinybert-l2 | 4.4 M | 17.6 MB | cpu | 0.07 s | 33.63 (46.82) ms | 5.34 ms | 76 MB (process peak 553 MB) | 51.52 → 89.97 ms |
| tinybert-l2 | 4.4 M | 17.6 MB | mps | 0.35 s | 8.98 (12.07) ms | 4.0 ms | 6 MB (process peak 485 MB) | 26.11 → 37.88 ms |
| minilm-l6 | 22.7 M | 87.6 MB | cpu | 0.1 s | 457.49 (638.04) ms | 32.67 ms | 132 MB (process peak 627 MB) | 46.58 → 396.42 ms |
| minilm-l6 | 22.7 M | 87.6 MB | mps | 0.44 s | 40.87 (251.61) ms | 65.59 ms | 7 MB (process peak 479 MB) | 50.35 → 132.7 ms |
| minilm-l12 | 33.4 M | 128.2 MB | cpu | 0.11 s | 960.16 (1245.45) ms | 30.07 ms | 166 MB (process peak 654 MB) | 51.36 → 826.71 ms |
| minilm-l12 | 33.4 M | 128.2 MB | mps | 0.47 s | 200.95 (518.31) ms | 17.46 ms | 9 MB (process peak 490 MB) | 24.39 → 298.04 ms |
| mmarco-mminilm | 117.6 M | 469.9 MB | cpu | 2.37 s | 864.05 (1005.9) ms | 50.05 ms | 638 MB (process peak 1118 MB) | 47.69 → 769.66 ms |
| mmarco-mminilm | 117.6 M | 469.9 MB | mps | 2.75 s | 319.71 (608.8) ms | 23.4 ms | 508 MB (process peak 981 MB) | 16.88 → 240.22 ms |

All models loaded and scored offline. "Weights on disk" is the downloaded safetensors plus tokenizer and config; the production embedding model is about 87 MB, so the chosen reranker would roughly double the package's model weight.

## 11. Does it actually improve performance?

No. Pooled over test questions it lowers right-first accuracy and MRR@10 for both the embedding and the hybrid retrieval, mostly because of Banking77, where it is clearly worse (77.7% → 64.5%). On the small FAQ-style sets it is mixed: better on some (including the paraphrase-heavy Pawfect set and the ambiguous questions) and worse on others, with 11–38 questions per set, so none of the small-set differences is strong evidence. Recall@20 cannot change by construction; Recall@5 and @10 are in the tables. It also adds latency, memory and a second model to ship, and it provides a score that must not be mistaken for confidence.

## 12. Recommendation

**Do not integrate** `ms-marco` cross-encoders as a drop-in reranker. **Experiment further** only if the result below is worth the cost:

1. The evidence engine inspects the top 5 FAQ entries and every passage, so the useful reranker target is *Recall@5* and the quality of the candidate *pool*, not right-first. Measure the evidence benchmarks with a reranked pool before anything else.
2. Combine instead of replace: interpolate the cross-encoder score with the retrieval score (not tested here), or use it only when the top retrieval scores are close (a margin trigger).
3. A reranker trained on FAQ-style (question, entry) pairs, or fine-tuned on a customer's own approved questions, could beat a web-search reranker; that is the only route with a plausible gain on Banking77-like data, and it was out of scope here.
4. A multilingual FAQ needs a multilingual model; the one tested (`mmarco-mMiniLMv2`) was the worst here despite being the only one trained for it.

## Reproduce

```bash
PYTHONPATH=src:. python -m experiments.cross_encoder.run                  # dev selection, test, locked (once)
PYTHONPATH=src:. python -m experiments.cross_encoder.variants            # the all-phrasings follow-up
PYTHONPATH=src:. python -m experiments.cross_encoder.performance         # latency and memory, offline
PYTHONPATH=src:. python -m experiments.cross_encoder.report              # writes this file
```
