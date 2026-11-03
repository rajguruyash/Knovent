# Provider and model selection: protocol, comparison, and status

**Status (2026-11-02): no model has been benchmarked through the production adapters, and no model is offered to customers.** The harness is complete and tested; the benchmark is **BLOCKED** on provider credentials that are not available to the shell where it runs (reported as `MISSING`, never printed), and local models were already shown too slow for the 8 GB development laptop. Nothing below is a result; it is the protocol, what is known, and what the first real run will produce.

## Providers the product can talk to

| Provider | Modes | Structured output mechanism | Terms verified for disclosure | Benchmarked here |
|---|---|---|---|---|
| Cloudflare Workers AI | platform | OpenAI-style `response_format` where the model supports it, else JSON-in-prompt | **no** | no (no credentials) |
| OpenRouter | platform, BYOK | OpenAI-style, with provider routing restricted to endpoints that support the parameters sent | **no** | no (no credentials) |
| OpenAI | BYOK | `response_format: json_schema` | **no** | no |
| Anthropic (Claude) | BYOK | a forced tool call whose input schema is the answer schema | **no** | no |
| Google Gemini | BYOK | `responseMimeType` + `responseSchema` | **no** | no |
| OpenAI-compatible server (custom address) | BYOK | JSON-in-prompt unless the server reports otherwise | **no** (the customer's own server; the disclosure says so) | no |
| Ollama / llama.cpp (the business's own machine) | local | Ollama `format`, llama.cpp OpenAI-style | not applicable (no third party) | no (hardware) |

"Terms verified" is the gate in `docs/provider-verification.md`: an entry is verified only when somebody has read the provider's official terms for the service tier in use and recorded retention, training use, abuse monitoring, contractual commitments, the sources and the date. **Zero of six entries are verified**, so no external provider can be offered, agreed to or called. Nothing here states what any provider's terms say.

## Candidate models (the platform could offer; none is offered)

From `inference/catalogue.json`, read from the providers' own model pages on 2026-11-01 and **to be read again before any is offered** (prices and availability change; a `null` is unknown, not zero):

| Provider | Model | Context | Price in / out (USD per 1M tokens) | Structured output | `platform_offered` |
|---|---|---|---|---|---|
| Cloudflare | `@cf/google/gemma-4-26b-a4b-it` | 262,144 | 0.10 / 0.30 | unverified | false |
| Cloudflare | `@cf/mistralai/mistral-small-3.1-24b-instruct` | – | 0.351 / 0.555 | unverified | false |
| Cloudflare | `@cf/qwen/qwen3-30b-a3b-fp8` | 32,768 | 0.0509 / 0.335 | unverified | false |
| OpenRouter | `mistralai/ministral-3b-2512` | – | 0.10 / 0.10 | json_schema (per listing) | false |
| OpenRouter | `qwen/qwen3.5-9b` | – | 0.17 / 0.25 | json_schema (per listing); reasoning disabled in the request | false |
| OpenRouter | `google/gemma-4-26b-a4b-it` | – | 0.0675 / 0.225 | json_schema (per listing) | false |

Local candidates (`companion/catalogue.json`) are listed in `docs/local-companion.md`; they are a configurable list, not benchmarked, with licences unrecorded.

A model becomes selectable for platform mode only when **all three** hold: the benchmark below passes the acceptance rules, its provider's terms are verified, and someone sets `platform_offered: true` on purpose. The router refuses any other model (`config:model_not_offered`), whatever a client asks for.

## What every model is given

The same frozen evidence per question (`runs/llm/evidence.fresh.json`, the two businesses' fresh sets, 20 + 20 questions, each captured by `llm_bench.py capture` with no model and no network and then concatenated, items first Acme, then Beacon): the question and numbered evidence items with immutable ids E1…En, the same system prompt, the same JSON schema. A model must answer in one JSON object (requirements, verdict `answered|partial|conflict|insufficient`, missing, answer with [E#] citations, claims with evidence ids). Hosted providers are called through the **production adapters** (`faqengine.inference`), so request building, parameter negotiation, the no-redirect/public-address rules and the failure classification are the production ones. The benchmark's prompt is the benchmark's own; the production composer's prompt (`runtime/compose.py`) is not identical, so a model that passes here still has to pass shadow validation through the real pipeline before it is offered.

Provider failures (rate limit, timeout, server error, bad credentials) are counted separately and never lower a model's accuracy; reasoning failures (invalid JSON, invented evidence ids, numbers in no cited evidence) are the model's.

## The seventeen measures

`llm_bench.py run` reports, under `metrics`: 1 overall correctness, 2 false-answer rate, 3 citation correctness, 4 abstention precision, 5 abstention recall, 6 unnecessary abstention, 7 conflict detection, 8 missing-information handling, 9 multi-source reasoning, 10 structured reasoning, 11 invalid-JSON rate, 12 fabricated-evidence-id rate, 13 provider failure rate, 14 median latency, 15 p95 latency, 16 cost per question, 17 cost per 1,000 questions. Measures 8–10 are the share correct within the data's own category labels (`missing`/`partial`, `multi_source`/`multi_condition`, `structured`/`comparison`/`price_calc`/`limit_check`…); a measure with no questions behind it is `null`, never zero. Cost is computed only when a price is given on the command line from the provider's page.

```
python scripts/llm_bench.py capture --data examples/unified   --set fresh --out runs/llm/evidence.unified.fresh.json
python scripts/llm_bench.py capture --data examples/reasoning --set fresh --out runs/llm/evidence.reasoning.fresh.json
# runs/llm/evidence.fresh.json is the two files' "items" lists joined (fields data/set/as_of kept from the first)
CLOUDFLARE_ACCOUNT_ID=… CLOUDFLARE_API_TOKEN=… python scripts/llm_bench.py run --evidence runs/llm/evidence.fresh.json \
    --provider cloudflare --model @cf/google/gemma-4-26b-a4b-it --price-in 0.10 --price-out 0.30 --json runs/llm/fresh.cloudflare.gemma4.json
```
(`scripts/run_llm_matrix.sh` runs the candidate list; credentials come from the protected environment file, never the command line.)

## The baseline to beat (measured, deterministic engine, no model)

On the 40 fresh questions (Acme 20 + Beacon 20; `runs/llm/fresh.deterministic.json`): correct **72.5%**, false-answer rate **19.4%**, citation correct 85.3%, conflict detected 40% (2 of 5), abstention precision 40% and recall 33.3%, missing-information questions 2 of 6, unnecessary abstention 3.4%. The weak categories are the ones a model is supposed to help with: `conflict` (2/5), `missing` (2/6), `multi_source` (1/3).

## Proposed acceptance rules (NOT yet agreed — the owner decides)

The brief asks for "the agreed acceptance threshold", and none has been agreed, so these are a proposal to react to, not a standard that has been met:

* **False-answer rate** ≤ 10% on the fresh set **and** lower than the deterministic baseline, with no category where it is worse than the baseline by more than one question
* **Citation correctness** ≥ the baseline (85.3%) and **fabricated evidence-id rate** reported (the verifier drops these claims, but a model that does it often is wasting calls)
* **Conflict detection** and **missing-information handling** not worse than the baseline
* **Invalid JSON** ≤ 5% and **provider failure** ≤ 2% on a run that is not rate-limited
* **p95 latency** within what a customer will wait (to be set; 15 s is the alert threshold), **cost per 1,000 questions** acceptable to the operator

The order of priority is the one in the brief: false answers first, then citations, conflicts and missing information, then reliability, latency, cost. If **no** model meets them, the finding is "no model is offered", and deterministic answering remains.

## Two sets, two roles

* The **fresh** sets (`examples/*/fresh.csv`, 20 questions each) become **model-selection data** by being used to choose a model. They are no longer independent evidence of generalisation and are not reported as such.
* The **final holdout** (`examples/unified/holdout.csv` — 24 Acme questions; `examples/reasoning/holdout.csv` — 25 Beacon questions; sha1 in `holdout.sha1`, checked by a test) was written **before any benchmark result was used for a decision**, from the businesses' own knowledge files, with no question equal to, or a close paraphrase (word overlap ≥ 0.7) of, one in the other sets (checked by program; the sealed sets were never displayed). It has **never been run**: not by the deterministic engine, not by any model. It is run **once**, with `--final`, for the model already chosen; the report is aggregate-only (no per-category breakdown, no per-question output), and each run is appended to `runs/llm/holdout.ledger.jsonl` so how often it was looked at is on record. Running it more than once, or choosing a model by it, makes it model-selection data like the others.

**Limits of the holdout, stated plainly:** one author wrote the knowledge, the other sets and this one, so the phrasing habits are shared; two small fictional businesses, English only; 49 questions are too few to separate rates a few points apart. Real customer questions are the better holdout, and the first thing to collect once a deployment exists.

## What remains blocked

1. **Credentials** for the benchmark (`CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_API_TOKEN`, `OPENROUTER_API_KEY`; reported `MISSING` by `scripts/env_status.py`) — the owner puts them in the protected env file; they are the benchmark's, not the service's (`PLATFORM_*`).
2. **Provider terms** read and recorded (`docs/provider-verification.md`).
3. **A decision** on the acceptance rules above.
4. **Suitable hardware** for any local-model measurement.
