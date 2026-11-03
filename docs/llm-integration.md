# Optional language-model integration

The language model is an **enhancement above** retrieval and evidence. It is never required for indexing, retrieval, evaluation, export or answering, and it never decides whether anything is supported. With no model every sentence below that says "the model may…" simply does not happen, and the answer is the stored or assembled wording.

## The onboarding question

First thing in a new project (Studio) and in a new business (hosted): *"Would you like to enable LLM integration for enhanced AI-generated answers?"*

* **No, use local-only mode** — complete setup. MiniLM embeddings, hybrid retrieval, evidence assessment, response composition, abstention. No key, no network.
* **Yes, enable LLM integration** — choose a provider, paste the key, **check the key and model** (the check lists the models the key can use, then makes one tiny request with the chosen model), choose what the model may do, save.

If the key is missing or wrong the page says what happened ("The provider rejected the API key…", "that model is not one this key can use", "the provider is not available right now") and offers three things: try again, change it, or **continue in local-only mode**. An unavailable provider never blocks onboarding (tested through the real HTTP calls against a stand-in provider). Provider, model and key can be changed later; it can be turned off and on; the key can be removed.

Supported: **OpenAI**, **Anthropic (Claude)**, **Google Gemini**, and **any OpenAI-compatible server** (Ollama, LM Studio, vLLM, a company gateway), which is also how a model that runs on the same computer is used. The connection code (`runtime/llm.py`, standard library only) speaks each provider's HTTP API; it was tested against stand-ins that reproduce each API's request and response shapes. **It has not been run against the real providers** (no keys here); an API change on their side would show up first as a "bad response" error in the check.

The interface tells the business what is sent where before it is turned on: the customer's question and the one stored answer or few supporting passages found for it go to the chosen provider; nothing else.

## What the model may do

Each is a separate switch, off or on per business.

| | Used when | Rules | Without a model |
|---|---|---|---|
| **Polish** | a stored FAQ answer already fully supports the question | rewrite it as a natural reply; no new fact; every price, number, date, time, condition and policy unchanged | the stored answer, verbatim |
| **Grounded combine** | several passages already support the answer | only the numbered passages; keep conditions and exceptions; cite `[1]`, `[2]`; no general knowledge | the passages' own sentences with references |
| **Retrieval assistance** | the first search left parts of the question unsupported | suggest up to three other search queries; untrusted text (short, plain, no instruction-like wording) | the honest partial answer or "not covered" |

Retrieval assistance only widens the candidates. They are then judged by the **same evidence engine against the customer's own question**, not against the model's reformulation, and the result is used only if it supports strictly more of the question. The model cannot make an unsupported question supported (tested: it suggests a search, the bath page is found, it has no price, the answer stays "not covered").

If the evidence is insufficient the model is **not called to answer** at all. If it is called and says `INSUFFICIENT`, that changes nothing the evidence established; the evidence's own wording is used.

## Verification: every model text is checked before it is used

`runtime/verify.py` is separate from the model, the evidence engine and the response engine. It takes the text and the evidence it was made from and compares **claim by claim, using typed facts**, not word overlap alone:

* **figures:** prices with currency, clock times, durations (business days kept apart from calendar days), percentages, dates, weekdays (ranges such as "Tuesday to Sunday" expanded; open days and closed days kept apart), plain numbers (including "1500s"), and per-rate units ("per hour" versus "per minute");
* **contacts:** web addresses, e-mail addresses and phone numbers that are not in the evidence (the usual way a planted instruction gets its link out);
* **polarity:** a yes turned into a no or the reverse, an opening "Yes."/"No." that differs from the evidence, a dropped "not", a list of opposites (free/charged, open/closed, before/after …);
* **commitments:** guarantees, "always", "never", "free", "unlimited"… that the evidence does not make; refund, discount, deposit, penalty… words it never mentions;
* **support:** most of the claim's words are in the evidence, or (with the embedding model) a close meaning together with 40% of the words. Meaning alone is never enough;
* **conditions:** an "if", "unless", "only", "except", "at least", "within 7 days"… in a sentence the answer relies on must survive, figures and the limiting word included (or already be in the customer's own question);
* **details (polish):** every figure of the stored answer must still be there;
* **references:** `[n]` must point at a real source.

Any issue fails the text. The issues are returned (type, the claim, what differs, which source) so the interface can show them. **A failed check is never silent:** the result carries `verification`, `fallback` (from what to what, why) and `llm` (tried, used, failure code, provider, model, tokens), and the Test screen shows them. What is said instead: the stored answer if sufficient, otherwise the passages' own sentences, otherwise a partial answer, otherwise the abstention.

It is a structured comparison, **not a proof**. It does not understand language: a claim that changes who or what a rule applies to without touching any figure, day, contact, yes/no, opposite or commitment word ("members" for "customers"), or that chains two true sentences into a false implication, can pass. It is strict on purpose: a valid rewording can be refused (a harmless closing pleasantry such as "Let me know if you need anything else" is refused, measured at 100% in `docs/llm-evaluation.md`).

## Injection

Documents and pages are data. Passages that speak to an AI ("ignore the previous instructions", role markers, "reveal your prompt", exfiltration links…) are quarantined before assessment: listed in the diagnostics with the reason, never searched, never sent to a model. Everything that does go to a model is fenced in tags with a random name, with a rules message that says the fenced text is data; and anything an obeyed instruction adds (a link, a price, a promise) fails verification. Both layers are tested; the patterns are heuristic and the verification is the real defence.

## Credentials

* Never returned: the interface and API report only "a key is saved".
* Never in a log line, an error message, a stored audit row, an exported package or a response (tests assert it after each path: invalid key, provider failure, redirect, timeout).
* Sent only in a request header to the provider's host. For Gemini, a header, not the address.
* **Hosted:** AES-256-GCM, bound to the business, key from the service's environment; rotation supported; a key copied to another business's row cannot be decrypted.
* **Local:** stored as it is in a file only the owner can read; not encrypted (the interface says so).
* Each business has its own configuration; changing provider drops the old provider's key.

## Cost and usage

Each answer reports calls, tokens in and out and time for the model's part; the hosted service stores them in `usage_records`. **A cost is reported only if a price per 1,000 tokens was configured;** otherwise it says no price was configured. Prices change, so none is built in.

## Not done

* A real provider has not been called in any test or measurement. "Working LLM-enabled mode, subject to valid credentials" means: the code path is tested end to end against stand-ins; a business with a real key should run the provider check and a few questions first.
* No streaming, no multi-turn memory in the prompt, no function calling.
* The check of a hosted business's key makes a real request, so it costs a few tokens.
