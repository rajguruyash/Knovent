# Provider verification checklist

A provider's API working is not a reason to send a business's text to it. Before a provider can be **offered** to a business (the consent screen shows it), **agreed to** (the service records the agreement) or
**used** (the service sends anything to it), a person has to read its terms and write down what they say, in `src/faqengine/hosted/disclosure.py` or the file named by `AI_DISCLOSURE_FILE`. The
service checks the result on every call: a provider with any item missing is **unverified**, and an unverified provider is neither offered nor agreed to nor used, whatever else is configured.

`python -m faqengine.hosted.disclosure` prints where every provider stands today and what is missing. **Today every provider is unverified: nobody has verified anything.**

## The checklist (one per provider, per service tier)

| Item (entry field) | What to record | What does NOT count |
|---|---|---|
| Official terms (`terms_url`) | The address of the provider's own terms, `https://`, for the page that applies to the tier below. | A blog post, a forum answer, a third-party summary, a page for another product. |
| Applicable service tier (`service_tier`) | The plan or product these terms apply to (a free tier and a paid tier often have different terms). | "Standard". Name the tier as the provider does. |
| Data retention (`retention`) | How long submitted text (the question and the evidence passages) is kept, where the terms say so, with the section. | "Unknown", "not stated", "unclear", "n/a", an empty field. |
| Training and data use (`training_use`) | Whether submitted text is used to train or improve models, and whether that depends on the tier or on a setting. | The same. If the terms are silent, the item stays open until the provider confirms in writing. |
| Abuse monitoring (`abuse_monitoring`) | What the provider does to monitor for abuse, and who can read submitted text for that purpose. | The same. |
| Regional processing (`regional_processing`, optional) | Where the text is processed, if the provider says. Leave it empty if the provider does not say; do not infer it from the provider's location. | Anything inferred. |
| Contractual commitments (`contractual_commitments`) | What the business can rely on: a data processing agreement, standard clauses, what is committed and what is only described. | A marketing page. |
| Date verified (`verified_on`) | The day the terms were read, `YYYY-MM-DD`. Not in the future. | |
| Who read them (`verified_by`, optional) | A name, so that a later reader knows whom to ask. | |
| Verification status | **Computed, never typed.** `verified` only when every required item above is an answer. | |

Write each answer in the provider's words where you can, with the section number. The text a business reads shows these answers as recorded, under "in the words of the person who read them", and
says the terms can change. Do not write what you expect a provider to do, what it did before, or what a similar provider does.

## Rules the service applies

1. **An item that is empty, or says "unknown", "not stated", "not specified", "unclear", "TBD", "n/a", "none found" or "no information", is not an answer.** The provider stays unverified and the text says what is missing.
2. **The address must be `https://` and the date must be a real day that is not in the future.**
3. **Editing an entry changes the disclosure text, and so its version.** Every earlier agreement to that provider stops applying; the business is asked again with the new text. (Agreements are bound to the version, and to a fingerprint of the exact text shown.)
4. **Verification is checked on every call, not only when an agreement is made.** If an entry is later edited so that an item is missing, the provider stops being used on the very next question, even though an agreement exists. A **withdrawal is always possible**, verified or not.
5. **A verified provider is still used only for a business that has agreed.** Deterministic mode, which sends nothing anywhere, never needs any of this.
6. **A business's own key (BYOK) does not change the rules.** The text sent still goes to a third party, so the provider is verified and agreed to like any other. (An OpenAI-compatible server on the business's own network is the same case until someone decides otherwise: the provider's entry says what was verified about *that* server.)

## How to verify a provider

1. Read the terms at the official address, for the tier you intend to use. Keep the address, the section numbers and the date.
2. Fill in every required item in an entry of `AI_DISCLOSURE_FILE` (a JSON object keyed by the provider's key; see the shape in `disclosure.PROVIDERS`). Leave anything you could not establish empty.
3. Run `python -m faqengine.hosted.disclosure`. The provider must say `verified`. If it does not, the list says which items are open.
4. Have a second person read the rendered text (`GET`/`POST /v1/ai/disclosure`) against the provider's terms. Do not skip this: a wrong "verified" is worse than none.
5. Only then is the provider offered. Provider integration, benchmarking and any real traffic are separate steps after this one, and none of them is a substitute for it.

## Status today

| Provider | Terms address | Tier | Retention | Training | Abuse monitoring | Regional | Contractual | Verified on | Status |
|---|---|---|---|---|---|---|---|---|---|
| Cloudflare Workers AI | – | – | – | – | – | – | – | – | **unverified** |
| OpenRouter | – | – | – | – | – | – | – | – | **unverified** |
| Google Gemini API (BYOK) | – | – | – | – | – | – | – | – | **unverified** |
| OpenAI API (BYOK) | – | – | – | – | – | – | – | – | **unverified** |
| Anthropic API (BYOK) | – | – | – | – | – | – | – | – | **unverified** |
| OpenAI-compatible server (the business's own) | – | – | – | – | – | – | – | – | **unverified** |

A dash means nobody has established it. Prices and availability found while choosing models for the benchmark (docs/llm-evaluation.md) are not terms and are not entered here.
