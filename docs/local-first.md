# Local AI first

**The principle: local AI first, free hosted second, paid hosted third. The customer owns their local inference capability, and cloud inference is an explicitly authorised fallback, never the default.** This page is the policy, how it is enforced, what the customer sees, what the hardware must be, and what has and has not been run on a real device.

Everything on this page is built and tested against the real database schema, the real embedding model and stand-in runtimes/providers over real HTTP, and **one real-device smoke test has now been run** (2026-11-02, below): a real Ollama 0.40.13 and a real `llama3.2:1b` (1.23 GB, downloaded once, through the companion) answered questions through the whole chain on an 8 GB Apple M2, with no outside request. That is a smoke test of one configuration, not a statement that local AI is production-ready, and it says nothing about answer quality beyond what is written there.

## What the customer chooses

| Choice (the setup screen's name) | Steps, in the order they are tried | Sends data outside the business? |
|---|---|---|
| **Local only** (`local_only`; what "enable AI" selects) | your computer | **Never** |
| **Local + free hosted** (`local_free`) | your computer → free hosted AI | Only if your computer cannot answer, to the free provider you agreed to |
| **Local + free + paid hosted** (`local_free_paid`) | your computer → free hosted → paid hosted (with your monthly spending limit) | Only if the steps before cannot answer, to a provider you agreed to |
| **BYOK as an alternative** (`byok_alternative`) | your computer → your own provider account (→ free → paid if also turned on) | Only if your computer cannot answer, to your provider |
| **BYOK as the preferred provider** (`byok_preferred`) | your own provider account (→ free → paid if also turned on) | Yes: your provider answers instead of your computer |
| **Deterministic only** (`deterministic_only`) | none | **Never**; no model at all |

The first step is the business's `mode`; the steps that may follow it are three switches that are all **off** by default (`fallback_byok`, `fallback_free`, `fallback_paid`) and always run in that order. Nothing the service, the browser or a failure does can add a step. The order is the same for every customer; what differs is what each turned on.

## The rules, and where each is enforced

1. **Local is always preferred when healthy and suitable.** Local is the first step of every policy except BYOK-preferred. It is *suitable* only if a companion is paired and has not said the chosen model is `not_advised` on this computer (`local:unsuitable_hardware`), and *healthy* is what the browser reports: if it cannot reach the companion or the model fails, that is reported to the service as a failure and the next permitted step is considered (`hosted/router.py`, `hosted/service.py:local_complete`).
2. **A step is used only if all of this holds, checked on the server for every request:** the operator switched its mode on (`ENGINE_AI_MODES`); it is configured; **its provider's terms are verified; the business agreed to *that provider's* text** (a different provider is a different agreement, a changed text needs a new one); its limits allow it; its circuit is closed. A step that fails any of these is **skipped with nothing sent** (read from the provider's side in `tests/test_local_first.py`).
3. **Turning a step on needs the agreement first.** Saving `local_free`, `local_free_paid` or `byok_alternative` is refused (409) unless the provider's terms are verified and the business has already agreed to them; the paid step also needs the business's own spending cap (400 without it). Withdrawing the agreement later stops that step at the very next request, and the answer lists it as refused.
4. **Free before paid.** Free hosted has its own monthly quota (requests and tokens) and spends nothing; a plan with no free allowance has no free hosted inference. Paid hosted stops at the **lower** of the plan's cost cap and the business's own spending cap (`limit:spend_cap`), is metered separately, and free calls never count against paid limits (`hosted/limits.py`).
5. **What advances to the next step, and what does not.**

   | What happened | Next step tried? |
   |---|---|
   | The step was refused (no consent, unverified terms, no quota, spending cap, circuit open, operator switch, no companion, unsuitable hardware, not configured) | **Yes**, if the business enabled one; nothing was sent to the refused provider |
   | The provider or the computer **failed** (timeout, network, server error, auth, rate limit, not connected) | **Yes**, if the business enabled one |
   | The person pressed **Cancel** | **No.** Nothing else is tried and nothing is sent anywhere |
   | A model **answered but its claims did not verify** (invalid JSON, an unknown evidence id, an unsupported claim) | **No.** A bad answer is not a reason to send the evidence elsewhere: the business's `fallback` setting decides (the deterministic answer, labelled, or "unavailable") |
   | No usable evidence, or the arithmetic was done in code | No model is asked at all |

6. **If no permitted step is usable,** the answer is the deterministic one, labelled as such with the reason (`failure_kind`), or "unavailable" if the business chose `fallback: none`. Evidence is never sent to a provider the business did not choose and agree to.
7. **No silent substitution.** The answer's record lists every step tried (`execution.attempts`), which one answered (`execution.tier`: `local`, `byok`, `free`, `paid`), who would have answered and why not (`execution.fallback_from`), and each hosted call is metered against its own tier. "Local" is claimed only for a reply that came back from the paired companion.

The same pipeline runs for every step (query understanding, unified retrieval, evidence validation, sufficiency, structured reasoning, composition, claim verification, citation checks); only the inference execution layer differs (`runtime/pipeline.py`, unchanged in its stages).

## How an answer is made when local is first

**Before any step is tried,** the service checks whether the engine already has a complete, verified answer (an exact FAQ entry or a calculation worked out in code). If so it answers at once and asks neither the customer's computer nor any provider (`answer_path: fast_path`, `provider_steps_tried: []`); the order of steps, consent, limits and the customer's settings are untouched, and a question that needs a model still goes local first (`docs/fast-path.md`). A real 1B model on the seven exact FAQ answers it was compared on was wrong or unusable five times in seven and took seconds to minutes; the fast path answered all seven in under 300 ms.

The service cannot run a model on the customer's computer; the browser does. So when the first usable step is local, the page asks the service to *prepare* the question (retrieval, evidence, prompt; nothing sent anywhere), passes the prompt to the companion on `127.0.0.1`, and returns the reply to the service to *complete* (claim-by-claim verification against the evidence the service kept). If the browser cannot reach the companion, or the model fails, it says so in the completion; **only then** does the service look at the steps after local, applying rule 2 to each. If local is not usable at all (no companion), the service goes straight to the later steps in the same request. A plain `respond` call that reaches a local first step answers deterministically and tells the caller local is available; it never forwards to a hosted step on the browser's behalf.

## The setup the customer sees

1. **Sign up, create a business, upload knowledge** (progress is shown; the knowledge is read and indexed as soon as it is uploaded; "Test it" then answers deterministically).
2. **"How should AI answer for this business?"** — *On my computer (recommended)*, *Through an API service*, or *No AI for now*. Each says plainly where the data goes.
3. **On my computer:** the page looks for the companion (a double-click launcher, `Start Local AI Companion.command`, shows the hardware recommendation and a pairing code), pairs, then offers **"Set up local AI for me"**: it names the recommended model (and its speed class), its approximate download size and the free disk space, and downloads nothing until the box is ticked. One click then downloads it from Ollama's library, selects it, and registers it. Several other small, fast models are listed with speed and fit; any can be chosen instead.
4. **Through an API service:** choose a provider and **drop the `.env` file or paste the key**. The file is read in the browser only for the five provider-key names it recognises (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, `OPENROUTER_API_KEY`); every other line (passwords, other services' keys) is skipped, nothing is uploaded or stored, the key is shown only as its last four characters, and it is encrypted on the server and never shown again. If a file holds several providers' keys, the person picks one and the others are discarded. The provider is then used only after the business agrees to its data terms.
5. **Test it:** every answer says who generated it (your computer / free hosted / paid hosted / your provider), and which earlier steps were tried and why they did not answer.
6. **Settings → How this business answers** can add or remove the outside steps at any time; each says what it sends, that it needs an agreement, that free hosted has quotas and may be unavailable, that paid may cost money (with the spending limit), and that external fallback can be turned off.

A `.env`, key, certificate or credentials file is **never accepted as knowledge**: the page refuses it before upload and the engine refuses it at reading (it would otherwise be indexed and quoted in answers).

## Hardware compatibility requirements

What the companion decides, from facts it can read (operating system, CPU, cores, memory, GPU and whether it shares memory); anything it cannot read is "unknown", never a guess. Fit is **estimated** from parameter count and quantisation (weights × bytes per parameter + 1 GB working memory), not measured, and says nothing about quality:

| Verdict | Rule | Meaning |
|---|---|---|
| comfortable | needs ≤ 30% of memory shared with the system (RAM, Apple silicon), or ≤ 60% of a discrete GPU's own memory | runs with the browser and other programs open |
| possible | ≤ 50% of shared memory, or ≤ 85% of GPU memory | runs, slowly, with other programs closed |
| not advised | more | would swap or fail; the router will not use a companion whose chosen model is `not_advised` |

Speed classes are a rule of thumb from parameter count, **not a measurement**: *fastest* (about 1B or fewer), *fast* (to about 2B), *balanced* (to about 4B), *capable* (larger, slower). On an 8 GB Apple-silicon laptop the list offers, as comfortable, qwen3-0.6b, gemma3-1b, llama3.2-1b, qwen2.5-1.5b, qwen3-1.7b, smollm2-1.7b and granite3.3-2b; as possible (slow), the 3–4B models (llama3.2-3b, qwen2.5-3b, ministral-3-3b, phi4-mini, gemma3-4b, qwen3-4b); and as not advised, the 8–12B models. The page starts with the largest *comfortable* model that is not slow (granite3.3-2b there), and a large machine still starts small and fast. **The catalogue is a configurable list, not a benchmark:** licences are not recorded, and the family and size tags were read in Ollama's public library on 2026-11-02 but not per quantisation. Under 12 GB of memory the companion says that hosted modes may serve the business better.

Practical minimums, stated as expectations and not as tested facts: a computer with at least 8 GB of memory for the 1–2B models; 16 GB or a GPU with 8 GB+ for 3–4B models to be comfortable; free disk of about 1.3× the model's download (the companion checks this before it downloads); Ollama installed and running (the companion does not install software). CPU-only answering works (`device: cpu`, no GPU layers) but takes seconds to minutes per answer.

## Model download and management: status

| Capability | Status |
|---|---|
| Companion installation | a double-click launcher and `faqengine companion run`; **no installer or signed application** (a packaged installer is not built) |
| Hardware detection | built; macOS Apple silicon run for real (and the real run's pre-flight used it); Linux/Windows parsers tested on recorded output only |
| Compatibility check and recommendations | built and tested; several small, fast options with speed class; one "start here" pick |
| Model download | built (through Ollama's own library); **one confirmation**, disk-space check, progress, selects the model when done; tested against the stand-in runtime **and run once for real** (llama3.2:1b, 1.23 GB, 277 s on this computer) |
| Storage management | free space and the space models take; list of installed models with sizes; remove (not the one in use) |
| Updates | "Update" re-pulls the tag and says whether the digest changed; manual (no automatic update, deliberately) |
| Switching | "Use" selects another installed model; the service is told the model and the companion's verdict |
| Runtime health | `ready / busy / no model / runtime not running / model missing`, whether the model is loaded, and counts of questions and failures (no content); refreshed on request and when the panel first opens |
| Cancellation | real: closing the socket to the runtime stops generation (measured on a real model: **0.01–0.05 s** to stop) |
| CPU fallback | explicit: "use the processor only" selects Ollama's no-GPU-layers option; **no automatic retry on CPU** after a GPU error (not built) |
| llama.cpp | supported as a runtime for models the person already has (`.gguf` files in the companion's folder); the companion does not download for it |

## Real-device results (2026-11-02): one computer, one model, a smoke test

`scripts/local_e2e_check.py --provision llama3.2-1b --yes --questions 4`, then again with the model installed. Raw reports: `runs/local-e2e/llama3.2-1b.json` (first run, with the download) and `llama3.2-1b.run2.json`.

| | |
|---|---|
| **Machine** | macOS 15.2, Apple M2, 8 cores, 8 GB, Ollama 0.40.13 (Homebrew bottle, 66.7 MB + the `mlx` library, 171 MB), run as a plain loopback process, not a login service |
| **Before downloading** | free disk 9.75 GB (10 GiB on the home volume); memory 65% free, 0.7 GB of swap already in use; model fit "comfortable" (needs about 1.7 GB); download size read from Ollama's registry manifest: **1,321 MB (1.23 GiB)**; the check refuses if disk < 1.3× the download. Nothing was removed or cleaned to make room (Homebrew's auto-cleanup and auto-update were switched off) |
| **Pairing** | paired over real signed HTTP; a wrong code, an unsigned request and a second use of the code were each refused (401, 401, 200, 401) |
| **Download** | `llama3.2:1b` only, through the companion's one-confirmation setup: **277 s** (about 4.4 MB/s on this connection); the companion selected it; installed size 1.23 GB; free disk afterwards 8.5 GB |
| **Selection, registration, health** | selected (`device: auto`), registered for a business on the `local_only` policy (steps: `local` and nothing else), health `ready` (and `loaded: true` after the first answer) |
| **Questions** (3 asked of the model + 1 the knowledge cannot answer, which the pipeline answered in code without asking the model) | run 1: model times 3.0 s, 14.3 s, 12.5 s (7–20 tokens/s); run 2: 1.0 s, 1.7 s, 1.6 s (**about 60 tokens/s**), whole-question times 1.1–1.8 s. The 10× difference between the runs is **not explained** (run 1 followed the download directly: the model was loading and the machine may have been busy; thermal state and background indexing are candidates) |
| **What the answers were** (not a benchmark: three questions, one author, and the pipeline's own verdicts) | for three questions the deterministic engine answers outright from an exact FAQ entry, the 1B model produced `partial`, `conflict` and, for the third, claims the evidence did not support, which verification dropped, so the deterministic answer was given and labelled `deterministic_fallback (reasoning:unsupported_claims)`. **Verification did its job; the 1B model did not improve these answers, and made two of them less complete.** Two runs gave the same outcomes. Small models need the benchmark protocol before they are called good, and the pipeline may deserve a rule for when the deterministic engine already has an exact FAQ answer (see below) |
| **Cancellation** | the answer was cancelled 1.5 s in: the runtime **stopped in 0.05 s and 0.01 s** (the socket close works on a real model); the companion reported `cancelled`, the service recorded `provider:cancelled` and a labelled deterministic answer, and the answer lists exactly one step, `local` |
| **Cancellation never triggers hosted fallback** | in these runs the policy had no hosted step (hosted fallback was not enabled, as instructed), so there was nothing to trigger; the case **with** free and paid steps turned on is covered by `tests/test_local_first.py` (a cancel sends nothing anywhere and lists one step) |
| **No external inference** | the operator switch had only `local` on, no provider credential existed in the process, the policy had no hosted step, every call was metered `local`, and this process's and Ollama's open connections were sampled twice a second: **run 2 saw only `127.0.0.1`.** Run 1 flagged a Cloudflare address: it was seen during and just after the download (Ollama's connection to its library closing), none was open afterwards (checked), and it did not recur in run 2. The sampler can miss a connection shorter than half a second, which is why the other evidence is recorded too |
| **Memory** | Ollama's processes peaked at about **300 MB resident** (the weights are memory-mapped, so resident size undercounts them); this process 165–168 MB; system free memory fell from 65% to 40% across run 1 (and 42% to 39% in run 2), about **1.2 GB of reclaimable memory**, consistent with the 1.23 GB model; swap did not grow (0.69 GB before and after); peak Ollama CPU 127–135% of one core |
| **Failures** | one of three asked answers lost its claims to verification (above); no provider failure, no timeout, no out-of-memory, no crash; the machine stayed usable. **The resources were adequate for a 1B model, with the browser pane open and 0.7 GB of swap already in use.** |

**Two defects this found, both fixed:** (1) the download's progress was the *last layer's* line, so after the large weights layer finished it jumped to a few hundred bytes; the companion now sums progress over layers (tested with a layered stand-in); (2) the first check run flagged a connection that belonged to the download's tail, so the sampler now attributes every external peer to its process and separates the download from everything else.

**Findings worth a decision (not changed):** when the deterministic engine already has an exact FAQ match, asking a small model to re-compose the answer made it worse in all three cases here; a rule that answers exact FAQ matches deterministically (or only lets a model "polish" when it passes a stricter check) would remove that risk. That is a product change to the pipeline's behaviour, so it is left to the owner after a proper benchmark.

## Remaining real-device tests

1. **Real update and remove** of an installed model (the buttons are tested against the stand-in only).
2. **CPU-only** answering (`device: cpu`) and a computer with a discrete GPU.
3. **A second platform** (Linux or Windows) for hardware detection and the companion, and a browser other than a Chrome-family one (untested). The real run drove the companion through its real signed HTTP service with the script playing the browser; the page's own IndexedDB/WebCrypto signing against a real companion has been exercised in a browser only over a stand-in Ollama.
4. **Larger models** (3–4B, "possible" on 8 GB) and **llama.cpp** as the runtime: not run.
5. **Answer quality of any small model on this product's task**: needs the benchmark protocol of `docs/model-selection.md`; this smoke test is not one.
6. **The hosted fallback end to end with a real provider** (free and paid), which needs verified provider terms and credentials.
7. **A longer soak**: many questions in a row, the model unloading and reloading, thermal throttling on this fanless laptop.

## What was reused and what changed

Reused unchanged: the provider adapters and registry, the resilience layer and circuit breakers, the pipeline and its verification, the consent tables and functions, the companion's pairing, signing, streaming and cancellation, the Settings screens' other panels, and every existing test (the single-step behaviour is exactly the old behaviour: a business with no follower switched on has one step and nothing else is tried). Changed: the router now returns an ordered plan (`Router.plan`; `Router.route` still returns the first step), the service walks the plan and records every attempt (`_respond_chain`), the local completion continues down the plan after a provider-level failure, the settings API takes policies and flags and validates each outside step against verification, agreement and (for paid) a spending cap, free hosted has its own quota and paid its own cap, the page's mode list became the policy chooser and first-run setup, and the companion gained a wider small-model list, model management, health, storage and one-confirmation setup. Migration 13 adds the flags, the cap, the free quota, a per-call tier and the hardware verdict (all default to off or empty, and are written only by the service).
