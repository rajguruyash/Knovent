# Shadow-mode validation (synthetic traffic)

> **This is not customer traffic and not a Supabase project.** Every request is a question written by this repository's authors or derived from its own fictional example knowledge, sent to the hosted
> service running on a throwaway local PostgreSQL. It shows whether the two answer paths AGREE and what the pipeline costs on this machine. It says nothing about live traffic, about a Supabase
> project, or about answer quality. `ENGINE_PIPELINE=on` was not enabled for anyone.

Run: 2026-11-02T06:22:58+00:00 · Darwin arm64 · Python 3.11.7 · businesses: acme, beacon, pawfect, bike, clinic, restaurant, saas, empty · embedding model: the shared pretrained model (no tuning)

## Result

| Measure | Value |
|---|---|
| Requests evaluated in `shadow` | **878** (12 to a business with no knowledge, which neither path runs on) |
| Shadow executions that completed | **866** |
| Shadow exceptions (the existing path answered) | **0** |
| Requests where both paths agreed on everything compared | **866** |
| Requests with at least one difference | **0** |
| …recorded in `audit_logs` (`pipeline.shadow_mismatch`) | 0 |
| Answer differences / claim differences / citation differences | 0 / 0 / 0 |
| Evidence differences (candidates, what was left out as not current) | 0 |
| Abstention / conflict / structured-result / tenant differences | 0 / 0 / 0 / 0 |
| Fallback frequency (pipeline fell back or failed, per pipeline run) | 0.00% (0 fallbacks, 0 errors) |
| Outcome counts identical with the pipeline off and in shadow | yes |
| Answer sources checked against the database | 1440 |
| …that belonged to another business | **0** |
| Answers (including cross-business questions) that retrieved or cited an entry outside the asking business's own index | **0** |

## Latency

Wall clock of the whole request (retrieval, answer, and the writes of the exchange to the database), per request, milliseconds. Every request was sent in both modes, in a random order, so the
overhead is the median and 95th percentile of the PER-REQUEST difference (shadow minus off); the two columns beside it are each mode's own median and p95.

| | n | median | p95 |
|---|---|---|---|
| `off` | 878 | 132.3 | 295.4 |
| **`shadow`** | 878 | **266.6** | **990.8** |

Overhead of running the pipeline beside the answer: **+137.3 ms median (+104% of the `off` median), +729.5 ms at the 95th percentile of the per-request difference**; p95 of `shadow` is +235% of p95 of `off`. Median difference in each third of the run (drift check): 134.7, 133.6, 140.7 ms.
The pipeline's own time, inside `shadow`: median 135.4 ms, p95 701.6 ms (n=866); the existing path's own time (both modes): median 73.2 ms, p95 239.4 ms.
In this version the pipeline runs inside the request, after the answer is made, so a sampled request waits for it: the overhead above is added to what a customer waits for on every sampled request (`ENGINE_SHADOW_SAMPLE` chooses the share; set it low for a live test). This machine is a throttled 8 GB laptop; a server will differ.

## Per business

| Business | Requests | Differences |
|---|---|---|
| acme | 197 | 0 |
| beacon | 262 | 0 |
| pawfect | 80 | 0 |
| bike | 174 | 0 |
| clinic | 51 | 0 |
| restaurant | 51 | 0 |
| saas | 51 | 0 |
| empty | 12 | 0 |

## What the two allowances do on this traffic

The comparison allows exactly two things (docs/reasoning-pipeline.md): extra candidates added at the END of the list by the pipeline's one targeted second retrieval, and a conflict's two sides read as claims on both paths. Without them the same traffic would have flagged **129** of 866 requests (evidence: 107, claims: 22). Everything beyond them is in the table below: the allowances hide nothing else.

## Differences, as recorded

None: no request had a difference in outcome, answer, claims, cited sources, conflicts, abstention, structured results, retrieved candidates or what was left out as not current.

## Reproduce

```bash
python scripts/shadow_validation.py --json runs/shadow-validation/report.json --markdown docs/shadow-validation.md
```
