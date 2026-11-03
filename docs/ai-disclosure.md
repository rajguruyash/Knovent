# Sending data to an AI inference provider: what a business agrees to

**Nothing is sent today.** No external inference provider is connected to this product, and deterministic mode (the default) answers inside the service. This page and the consent records exist so
that the first provider is attached behind a real agreement, not a toggle.

## What the agreement is
A business's owner or admin agrees, for **one named provider** and **one version of the disclosure text**, that two kinds of data may be sent to that provider when a question is answered with AI:

1. the question a customer asks;
2. the passages of the business's own knowledge that were retrieved for that question and are needed to answer it (FAQ answers, document passages, spreadsheet or JSON rows, web pages).

Never sent: the whole knowledge base, another business's data, API keys or credentials, the identity of the customer (unless they typed it into the question).

## Only a provider whose terms were verified is offered
A provider is **offered** on the consent screen, **agreed to**, and **used** only if a person has read its terms and recorded every item of `docs/provider-verification.md` (address, tier, retention, training use,
abuse monitoring, contractual commitments, the date). Anything missing, or answered "unknown" or "not stated", leaves it unverified, and an unverified provider is not shown as something to agree to. The service checks
this on every call, so an entry that is later edited into an unverified state stops the provider being used at the very next question, even if an agreement exists. **Today no provider is verified**, so the screen
offers nothing and says why.

## What the provider does with it is the provider's business
The provider is a separate company. Its retention, who can read the text and whether it is used to train models are governed by its own terms. This service **has not verified any provider's terms**:
every provider entry in `src/faqengine/hosted/disclosure.py` (or the file named by `AI_DISCLOSURE_FILE`) has `terms_url`, `verified_on`, `retention` and `training_use` empty until someone reads the
provider's terms and writes down where and when, and the text a business reads says so in words for as long as they are empty. Nothing on this page or in the product promises anything about a provider.

## Deterministic mode
Deterministic mode, and any business that has not agreed, never sends evidence to an inference provider. Retrieval, the evidence checks, structured calculations and composition run in this service.

## Where a business agrees
Settings → "AI providers and your data" (the browser calls `/api/ai/disclosure`, `/api/ai/consent/get`, `/api/ai/consent/grant`, `/api/ai/consent/revoke`; the Vercel functions check the person's sign-in and role
and forward only the provider and the version of the text that was shown). The page shows the exact text the service returns, the provider (named as a third party), the disclosure version and what may be sent, and
needs an explicit tick before "Agree". The page cannot say that someone consented: the service records the signed-in person, compares the version with the current text and refuses an unverified provider (409).
Members who are not owners or admins can read but not agree or withdraw.

## Revoking
An owner or admin can revoke at any time (`POST /v1/ai/consent/revoke`; "Withdraw agreement" on the screen). It takes effect on the **next question**: the service asks the database before every external call. Text already sent cannot be
recalled by this service. The record of the agreement and its revocation (who, when, which provider, which version) is kept.

## How it is enforced
* The table `ai_inference_consents` (migration 10): one row per grant, revoke-only afterwards, no deletes, only owners and admins may grant or revoke, other businesses cannot see it (row level security), no credentials in it, every change in the audit trail.
* The **service** checks it, on the server, before every external call (`hosted/service.py: DbConsent`). The browser, the Studio and the web functions are not trusted to check.
* Consent is per provider and per disclosure version: a different provider, or a changed disclosure text, is not covered by an earlier agreement.
* The agreement stores the version and the SHA-256 of the exact text shown, so "version X" can be checked against what was displayed.
