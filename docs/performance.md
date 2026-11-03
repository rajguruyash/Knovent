# Performance measurements

Measured on 2026-11-02 with `scripts/perf_measure.py`: the real hosted engine, called in-process, against a throwaway local PostgreSQL 18 with every migration, the real embedding model (MiniLM-L6, ONNX), synthetic FAQ knowledge (distinct codes and values), and a stand-in provider that answers instantly. Raw output: `runs/perf/perf.json` (second run) and `runs/perf/perf.run1.log` (first run's log; its JSON was overwritten by the second).

**The machine matters more than usual.** Apple silicon laptop, **8 GB memory, 8 cores, fanless**, which throttles under sustained load (earlier work on this machine saw sustained throughput fall to a fifth of burst). No network hop, no TLS, no Vercel function start, a local database with no round trip. These are the engine's own costs on a weak machine, not a capacity promise for a server; a hosted database adds its round-trip time to every query (six connections per answer, below).

## Results (two full runs, same script, same machine, same day)

Answer latency (whole `respond`, deterministic mode, 40 questions per size; "exact" repeats a FAQ question, "reworded" prefixes it):

| FAQs in the business | Run 2 p50 / p95 (exact) | Run 1 p50 / p95 (exact) | Reworded p50 (run 2 / run 1) | Ingest (FAQs per second, run 2 / run 1) | Process memory |
|---|---|---|---|---|---|
| 200 | 204 / 550 ms | 215 / 774 ms | 196 / 196 ms | 68 / 57 | 248 MB |
| 1,000 | 280 / 900 ms | 401 / 888 ms | 203 / 203 ms | 36 / 37 | 211 MB |
| 3,000 | 302 / 938 ms | 455 / 1,331 ms | 251 / 251 ms | 40 / 37 | 409 MB |

Every reworded question was answered with the right value (40/40 at each size) — which says the synthetic data is easy, not that retrieval is accurate; accuracy is measured on the evaluation sets, not here. First question after an ingest (builds the business's in-memory search index): 0.2 s at 200, 0.4 s at 1,000, 1.2 s at 3,000 FAQs. Embedding model load: 0.06 s (cached). Peak process memory over the whole run: 428 MB.

Concurrency on the 3,000-FAQ business (threads share one process; requests are exact FAQ questions):

| Threads | Throughput run 2 / run 1 | p50 | p95 | Peak database connections |
|---|---|---|---|---|
| 1 | 3.0 / 2.3 req/s | 282 ms | 620 ms | 1 |
| 4 | 3.7 / 2.9 | 1,053 ms | 1,894 ms | 4 |
| 8 | 4.9 / 3.8 | 1,468 ms | 2,377 ms | 8 |
| 16 | 5.7 / 4.5 | 2,659 ms | 3,731 ms | 10 |

No request failed in either run. Throughput barely scales with threads: **one engine process behaves as CPU-bound** (the cause was not profiled under concurrency; the Python parts of ranking and classification share one interpreter lock, which is the likely reason), so more traffic needs more processes or instances behind the load balancer, not more threads. The connection cap (20 per process) was never reached: 16 concurrent requests held at most 10 connections.

What the model path adds (BYOK against a stand-in that answers at once, 40 questions on the 3,000-FAQ business; 32 reached the model, 8 were answered without one because the evidence needed no composing): deterministic p50 **196 ms**, BYOK p50 **313 ms** (p95 383 vs 539 ms). So the pipeline, the consent, limit and routing checks, the decryption and one local HTTP call cost roughly **120 ms at the median** here; a real provider's own time (typically seconds) is added on top and is not measured. (Run 1 showed 197 vs 357 ms, but its model-path figure came from a loop that switched modes between calls and **does not measure the model path**; it was found and fixed before run 2.)

## Where the time goes (profile of one 1,000-FAQ business, 30 answers, isolated, same machine)

74 ms per answer in that short isolated run, of which: **46% is the embedding model** (about 18 ONNX calls per answer, ~2 ms each, made while the evidence rules classify candidates), ~19% just opening database connections (six per answer, 2.3 ms each on a local socket — **no connection pooling**; against a hosted database each connection is a network round trip and a TLS handshake, so a pooler (Supabase's) is not optional), the rest ranking and composition.

## What these numbers do and do not say

* The same 1,000-FAQ business answered in **74 ms** in the short isolated profile and in **280–400 ms** in the long runs. The cause was **not** established. Candidates: thermal throttling after minutes of sustained load (known on this machine), PostgreSQL's background work after bulk inserts into the vector index, memory pressure on 8 GB. Read the table as an order of magnitude (tens to hundreds of milliseconds per answer, a few requests per second per process on this hardware), not as a latency guarantee.
* **Ingestion is embedding-bound** (about 40–70 FAQs per second here); a 3,000-FAQ file takes about 75 seconds. Uploads are processed inside one request (the Vercel function allows 150 s), so a very large file should be split; a background queue is not built.
* **Not measured:** a real provider's latency and failure rate (no credentials); a hosted Supabase's latency; tens of thousands of entries in the hosted service (the Studio's separate figures: search 0.8 / 8.6 / 72 ms at 1,000 / 5,000 / 20,000 entries, `docs/production-readiness.md`); documents (the synthetic knowledge is FAQ rows only, `chunks` = 0); any load from many businesses at once (one business was measured at a time, and the in-memory index of every active business is held in each process); browser rendering.
* **Not changed:** retrieval, embeddings and evidence classification were not touched to speed anything up (rule: no casual change to retrieval behaviour). Batching the ~18 embedding calls per answer is the obvious saving and would need the benchmarks re-run first.

Re-run: `.venv/bin/python scripts/perf_measure.py [--sizes 200,1000,3000] [--questions 40] [--threads 1,4,8,16]` (about 12 minutes on this machine; needs a local PostgreSQL with pgvector).
