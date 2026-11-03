# Reference study: vaizent.com, and how we upscale it

Source: Vaizent's public pages (home, product, features, solutions, pricing, docs), read on 2026-10-31.
No account was created and nothing was submitted. Plan numbers are indicative: Vaizent's own pages
disagree with each other on some limits (PDF size, pages scraped, "unlimited" on the top plan).

**Rule for using this reference:** match the feature set and the flows people already understand. Do
not copy their wording, branding, visual identity, assets or code. Our name, copy, design and API
naming are original.

## What Vaizent is

A hosted, multi-tenant "agent builder for websites". A business signs up, builds a chatbot in a
wizard, trains it on its own content, pastes one script tag on its site, and watches conversations and
leads arrive. Under the hood (their own description): retrieval-augmented generation, 384-dimension
vectors in a hosted vector database, and an OpenAI-compatible LLM that generates the final reply. Nothing public says a confident match can skip the LLM, and pricing is per AI reply. The
widget is one dependency-free JavaScript file. WhatsApp is connected by automating WhatsApp Web.

## Feature map: what we match, what we upscale

| Area | What Vaizent offers | Ours (match) | Ours (upscale) |
|---|---|---|---|
| Agent wizard | 10 templates, 8 tone presets, system prompt plus an "AI writes it" button, welcome message, language, Draft to Active status | Same wizard, original templates | Templates ship a starter FAQ set and a recommended base model; the wizard shows a training-time estimate for this device |
| Knowledge base | Text, FAQ builder, PDF, URL scrape (page and size limits per plan) | Same four sources | Data is ingested on the customer's device; duplicate and conflicting-FAQ checks; AI-suggested paraphrases per entry using the customer's own LLM key |
| Answering | Vector search, then an LLM generates the reply (nothing public says a confident match skips it) | Retrieve, then answer | **Retrieval-first**: above a calibrated confidence the stored answer is returned with no LLM call; LLM only in the middle band; honest "don't know" below a floor. Fewer LLM calls is the cost story |
| Model | General embedding model, shared across tenants | Embeddings plus index | **Per-business fine-tuned embedding model** (and later a reranker), trained locally, with measured lift over the untuned model and BM25 |
| Widget | One script tag, 5 themes, 12+ icons, 3 shapes, brand colour, desktop/mobile preview, session persistence, pre-chat lead form with skip, text and voice modes | Same | Shows the matched FAQ and a confidence cue; thumbs up/down feeds the next training run; optional in-browser engine for static sites |
| Leads | Configurable fields (name, email, phone), AI intent summary, duplicate detection, CSV, WhatsApp forwarding, webhook | Same | Intent comes free from the matched FAQ entry (no extra LLM call per lead) |
| Analytics | Volume, lead rate, usage meters, per-agent breakdown, searchable history, CSV | Same | **Retrieval analytics**: unanswered and low-confidence questions clustered into "add this FAQ" suggestions, per-entry hit rates, direct vs LLM vs fallback split, cost saved |
| API and webhooks | Per-agent API key, `POST` chat endpoint (message, sessionId, history), 60 requests/min, HMAC-signed webhooks for 3 events with retries | Same shape, own naming | A `retrieve` endpoint (chunks plus scores) for other people's agents; the exported runtime exposes the same API, so moving from hosted to self-hosted is a URL change |
| Channels | WhatsApp via WhatsApp Web automation, voice minutes | Later | Official WhatsApp Business Cloud API (browser automation is fragile and likely against WhatsApp's terms); browser speech for a free voice path |
| Security | Domain verification, encrypted storage, key rotate/disable | Same | Knowledge stays on the device by default; consent screen for anything sent to a cloud service |
| Languages | English, Hindi, regional | Multilingual base option | Per-language evaluation and a Hinglish test set |
| Pricing | Free (1 agent, 150 messages/month), then three paid tiers, INR via Razorpay or USD, white-label from the third tier | Tiered plans | Price on engines trained and exports rather than per message, because retrieval-only answers cost almost nothing. **Decision pending** |
| Agencies | One agent per client, white-label, API-driven | Multi-workspace | Per-client exportable engines |
| **Not in their public material** | Evaluation metrics, local training, model download, abstention calibration | n/a | **Our wedge.** Hardware panel, live training dashboard, P@K / R@K / MRR / nDCG report, golden-question regression tests, downloadable engine |

## Where the upscale is real, and where it is a hypothesis

- Independent of any training result: retrieval-first answering with calibrated thresholds, knowledge-gap
  analytics, citations and feedback, the exported runtime, the official WhatsApp route.
- Depends on Phase 0: that fine-tuning on a small FAQ beats an untuned model plus BM25. Phase 0 exists
  to answer this before the studio is built. Results are in `docs/RESULTS.md`.

## Roadmap, re-cut around the Vaizent surface

| Phase | What ships | Vaizent surface it covers |
|---|---|---|
| 0 | CLI: FAQ file, fine-tune, evaluate against baselines, export a runnable package | none (the differentiator, proved first) |
| 1 Studio | Local agent plus web UI: agent wizard, FAQ builder and CSV import, hardware panel, live training dashboard, test console, evaluation report, export button | wizard, knowledge base, "train on your data" |
| 2 Deploy | `widget.js`, runtime API (retrieve and chat), leads, webhooks, API keys, domain verification, retrieval analytics | widget, leads, analytics, API, security |
| 3 Depth | Reranker, bring-your-own-LLM orchestration, PDF and URL ingestion, LangChain / LlamaIndex / MCP snippets, WhatsApp, voice, multilingual | channels, languages, remaining knowledge sources |
| 4 Business | Marketing site (home, product, features, solutions, pricing, docs, contact: original copy), accounts, billing (Razorpay and Stripe), desktop installer, in-browser engine | the public site and pricing |

## Phase 1 studio: the screens

Vaizent's own onboarding is four steps (create the agent, train it on your data, embed it, watch leads and
insights). Ours keeps that rhythm and adds the part they do not show.

| Screen | Contents | Vaizent counterpart | Added by us |
|---|---|---|---|
| Agents | List with Draft / Active status, one card per agent | Same | Last trained, last evaluation score |
| New agent | Template, then name, tone, welcome message, language | Wizard | Template carries a starter FAQ set |
| Knowledge | FAQ builder (question, other phrasings, answer, category), CSV / JSON import, pasted text | Text, FAQ builder | The `check` validation inline, near-duplicate and conflicting entries flagged, a "more phrasings" nudge because it is the biggest quality lever |
| Train | Device and hardware panel (CPU, cores, RAM, free disk, GPU / MPS / CUDA), a time estimate before starting, then live stage, step, train and dev loss, steps per second, memory, ETA, warnings, stop | None | Everything on this screen |
| Evaluate | The report: tuned vs untuned vs BM25, lift with intervals, answer bands, entries that fail most, test questions that failed | Message counts only | Everything on this screen |
| Test console | Type a question, see the closest entries, their scores, the matched wording and which band it falls in | Preview chat | Scores and the reasoning, not just a reply |
| Deploy | Script tag and theme editor (Phase 2), API keys, domain allow-list, export package | Embed tab, API tab | Export is a first-class button |

### What the built studio does beyond this table (2026-10-31)

- **Several inputs, not just a FAQ.** The first tab takes several files at once: FAQ tables (flexible column names), documents (txt, md, html, pdf, docx) and web pages (paste an address; optionally follow links on the same site) cut into passages that give the engine context but are never answered from directly. Files can be added, replaced and removed later; a banner says when the trained model is out of date.
- **Held-out questions.** An optional typed CSV replaces the generated sanity checks with a measured, leakage-checked evaluation (by question type, with refusal validated separately and "did training help?" with an interval). See the README.
- **Configurable confidence lines.** "Not in the FAQ" and "answer without an LLM" can be set by hand on the Test tab.
- **It keeps learning.** Approved or corrected questions join the search at once; the exported package can be taught more while live, and `ci/improve.py` rebuilds it from its own sources with a promote-only-if-not-worse gate and a rollback (`src/faqengine/ci/README.md`). This is the part of "deploy" that the table's Vaizent column has nothing for.

The studio is served by the local trainer itself (the Jupyter pattern), so the browser talks to the same
machine and there are no mixed-content or localhost-permission problems. The trainer already writes the
event stream the Train screen needs (`events.jsonl`: stage, step, loss, learning rate, samples per second,
memory, ETA).

## Open decisions

1. **Product name and brand.** "faq-engine" is a working folder name only.
2. **Local-first vs a hosted widget.** A widget on a public site needs a server that receives
   conversations and leads. "Data stays on the device" is satisfied by the exported runtime on the
   customer's own server; a hosted runtime is a convenience tier and needs a consent flow.
3. **Pricing basis.** Per engine and export, or per message like Vaizent. The retrieval-first design
   makes the first possible.
4. **Studio stack.** Python (FastAPI) for the trainer, plus React, Vite and Tailwind for the UI to match
   the WASHO stack.
