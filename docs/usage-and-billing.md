# Usage, limits and billing

What is metered, what is limited, who can see it, and what is **not** built. Prices are not decided in this repository: no price list, no plan price and no margin appears anywhere in code or data, and there is **no payment system** here (the product has one planned elsewhere and this repository does not add a second one).

## What is recorded

Every call that reaches a model, and every ingestion, writes one row to `usage_records` from the server (businesses cannot write it): `kind` (`llm_call`, `ingest`), `mode` (`deterministic`, `platform`, `byok`, `local`), `provider`, `model`, tokens in and out, **estimated cost**, latency, `status`, `generated_by`, `failure_kind` and the `correlation_id` of the request. A refused or failed attempt is a row too (status and kind say why), so usage and failures can be reconciled.

* **Cost is an estimate from a configured price, not an invoice.** It is computed from the token counts the provider returned and the price in `inference/catalogue.json` (or the price given for a custom model). A model with no recorded price has cost `null` — *unknown*, not zero — and a `null` is never added to a total as zero. Token counts for local models are as the runtime reports them; local use has no money cost to the platform.
* Platform use is counted in the **calendar month in UTC**.

## Free hosted and paid hosted are separate steps with separate limits

Under the local-first order (`docs/local-first.md`) the platform's hosted inference is two steps, tried in this order and only if the business turned them on and agreed to each provider:

* **Free hosted** (a catalogue model whose tier is `free_trial` or `best_effort_free`): its own monthly quota on the plan (`free_requests_per_month`, `free_tokens_per_month`; empty means **no free hosted inference**), metered as tier `free`, never costs money, may be unavailable (`limit:free_quota_requests`, `limit:free_quota_tokens`, `limit:no_free_quota`, or a provider failure).
* **Paid hosted** (tier `paid`): the plan's request, token and estimated-cost limits **and the business's own monthly spending cap**, whichever is reached first (`limit:spend_cap`); a business cannot turn it on without setting a cap; metered as tier `paid`. The estimate is made from the call before it, so a single call can pass the cap by its own cost.

Free calls never count against the paid limits, and calls from before tiers existed count as paid. Each tier is a column on every call's usage record, so the screen shows free, paid, your own provider and your own computer separately.

## Three kinds of platform access, kept apart

`business_plans.plan` is one of:

| Plan | Meaning | Ends |
|---|---|---|
| `trial` | a free trial of platform inference with explicit limits | at `trial_ends_at`: the next request is refused with `limit:trial_ended` |
| `free` | best-effort free access with explicit limits; no guarantee of capacity, and the first thing the platform may reduce | never by itself |
| `paid` | access under limits the operator set for what the customer pays for | when changed |

The labels are what a customer is told in Settings → Usage; **their commercial terms are not defined here** (no price, no SLA). A business with **no plan row, or a plan with any of the three monthly limits left empty, has no platform inference at all** (`limit:no_plan`): empty is not "unlimited", because unlimited has no cost model.

## Limits, in the order they are checked (`hosted/limits.py`)

1. **Per minute, per business, every mode that reaches a provider** (abuse control; plan value 1–600, default 20): `limit:rate_limited`
2. Own key (BYOK) and own machine (local) stop here: the provider's terms and the customer's own account apply to BYOK, and nothing is billed for local.
3. **Platform only:** a plan with all three monthly limits stated; the trial's end; requests per month (`limit:quota_requests`); tokens per month (`limit:quota_tokens`); estimated cost cap per month (`limit:quota_cost`).
4. **Shared capacity:** platform calls share a concurrency cap so no business can use all of it (`limit:platform_busy`).

All checks read usage from the database, so they hold across several instances of the service; a few concurrent requests can pass a limit together, by at most the number in flight. A refused request is answered deterministically (or "unavailable" if the business chose `fallback: none`) with the limit named in `execution.failure_kind`, and the customer sees what is left in Settings → Usage and plan.

## Who can see and change what

| | Owner/admin | Editor | Viewer | The engine service |
|---|---|---|---|---|
| Usage screen and totals (through the service) | yes | no | no | yes |
| The plan row itself (through the database's row-level security) | yes | yes | yes | yes |
| Read how answers were generated (History) | yes | yes | yes | — |
| Change mode, provider, model, fallback | yes | no | no | — |
| Change a plan | **no** — plans are written by the operator (the database) | no | no | — |

A business cannot raise its own limit: `business_plans` has no write path from a customer session (RLS and grants, `tests/test_ai_modes_schema.py`).

## What is not built

* **Billing and invoicing.** No invoice, no payment, no price list. When a payment system exists, it should write `business_plans` (plan and limits) and read `usage_records`; nothing else needs to change.
* **Self-service plan changes** and in-product upgrade prompts beyond the message that a limit was reached.
* **Cost reconciliation against the providers' own invoices.** Estimated cost will differ from an invoice (rounding, minimum charges, price changes since `verified_on` in the catalogue).
* **Per-user quotas** (limits are per business).
* **Alerts to the customer** when they near a limit (the operator is alerted when a business hits one: `quota_refusals`).

## Tests

`tests/test_ai_modes.py` (35): metering per mode with token counts and estimated cost; the monthly request limit, the token limit and the cost cap each stop the next call and say so; a plan with a missing limit allows no platform inference; the trial ending; the per-minute limit for BYOK and local; usage not crossing between businesses; failures recorded with their kind; `tests/test_ai_modes_schema.py` (17): who can read and write plans and usage. The Settings usage screen: `web/tests/modes_ui.test.js`.
