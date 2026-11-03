# Local AI: the desktop companion

Local mode lets a business answer with a model running **on its own computer**. The evidence never goes to an inference provider: the hosted service hands the prompt to the person's browser, the browser hands it to a small program on the same computer (the *companion*), the companion asks Ollama or llama.cpp on loopback, and the reply goes back to the service, which verifies every claim against the evidence it kept. The architecture is in `docs/architecture.md`; this page is the companion, what it protects, and what has and has not been run.

```
companion run                     # starts on 127.0.0.1:47890 and prints a pairing code (5 minutes, single use)
companion recommend               # what this computer can honestly run (read-only system commands)
companion status                  # which runtimes and models it found
companion pairings [--remove ID]  # list or remove paired browsers
```
(`python -m faqengine companion …`; `--origin` adds the hosted app's address to the origins allowed to ask for a pairing; `--allow-custom-models` lets a paired browser ask Ollama to download a model that is not in the companion's list. Both are off by default.)

## Supported platforms — stated exactly

| Platform | Status |
|---|---|
| **macOS on Apple silicon** | the one the companion was written and run on: hardware detection, the HTTP service, pairing, signed requests, streaming and cancellation have run here **against stand-in runtimes** (`tests/fake_runtimes.py`: real HTTP servers that speak Ollama's and llama.cpp's protocols) |
| Linux (`/proc`, `nvidia-smi`) | parsers tested on **recorded output only**; never run on a Linux machine |
| Windows (PowerShell, `nvidia-smi`) | parsers tested on **recorded output only**; never run on a Windows machine |
| Real Ollama end to end | **run once** (2026-11-02: macOS 15.2, Apple M2, 8 GB, Ollama 0.40.13, `llama3.2:1b`): pairing, one-confirmation download, selection, registration, health, real questions and a real cancellation, with no outside request (`docs/local-first.md`, "Real-device results"). **llama.cpp: not run** |

So "local AI works on explicitly supported configurations" is claimed for **one configuration only, as a smoke test**: macOS on Apple silicon, 8 GB, Ollama 0.40.13, `llama3.2:1b`. The chain works there (signed pairing, download, selection, health, verified answers, cancellation in 0.01–0.05 s), but the small model's answers were not better than the deterministic ones (`docs/local-first.md`), nothing else (other models, llama.cpp, Linux, Windows, a GPU, CPU-only) has been run, and quality is not benchmarked.

## Managing models on this computer (`companion/server.py`)

Beyond pairing and answering, the signed API offers: `GET /v1/models/installed` (models with sizes, which is in use), `GET /v1/storage` (free disk and the space models take), `GET /v1/health` (`ready`, `busy`, `no_model_selected`, `runtime_not_running`, `model_missing`; whether the model is loaded; counts of questions and failures and the last answer's time, never what was said), `POST /v1/models/select` (with `device: auto|cpu`), `POST /v1/models/delete` (not the model in use), `POST /v1/models/update` (re-pulls a tag and reports whether it changed) and **`POST /v1/provision`**, the one-confirmation setup: with `confirm: true` exactly it picks the recommended model (or one chosen from the list that fits), refuses if Ollama is not running or the disk has under 1.3× the download free, downloads it through Ollama, and selects it; without the confirmation it does nothing. It does not install Ollama or any other software. `docs/local-first.md` has the whole picture and the status of each.

## Choosing a model honestly (`companion/hardware.py`, `models.py`)

* hardware: operating system, CPU, cores, memory, GPU (and whether it shares memory with the CPU, as on Apple silicon). Anything that cannot be determined is `None`, never a guess.
* candidates come from `companion/catalogue.json` (configurable with `COMPANION_MODELS_FILE`): **fifteen models, with several small, fast options** (qwen3 0.6b/1.7b/4b/8b, gemma3 1b/4b/12b, llama3.2 1b/3b, qwen2.5 1.5b/3b, smollm2 1.7b, granite3.3 2b, phi4-mini, ministral-3 3b), each with a *speed class* (fastest, fast, balanced, capable; a rule of thumb from the parameter count, not a measurement). **They are not a recommendation**: nothing in the list is benchmarked on this product's task, licences are recorded as `null`, and the family and size tags that carry a `verified_on` date were read in Ollama's public library listing that day (not per quantisation); the older entries are unchecked. Read a model's licence yourself.
* `recommend` also returns `suggested` (the best runnable pick for each speed class) and `start_with` (the largest comfortable model that is not slow, else the smallest possible one), so a person is offered several options and one place to start.
* fit is **estimated** from the parameter count and quantisation (`estimate_gb`), judged against GPU memory where there is a dedicated GPU, otherwise against all system memory: `comfortable`, `possible` (slow, other programs closed) or `not_advised` (would swap or fail). Under 12 GB the recommendation says that only the smallest models are realistic and that a hosted mode may serve the business better; if nothing fits it says local mode is not advised there.
* the choice is stored with the companion registration (runtime and model name only) and shown in Settings → AI.

## What protects it

The companion is a local HTTP service, so the question is who else on the computer, or what web page in the person's browser, can talk to it.

* **Loopback only.** It binds to `127.0.0.1`. Runtimes it talks to are checked to be on loopback too.
* **Nothing answers without a signature except the handshake.** `hello` (is a companion here, and is this browser paired) and the pairing exchange are the only unsigned calls; `hello` returns no prompt, no model output and nothing about other pairings.
* **Pairing.** A short code printed in the terminal, valid 5 minutes, used once (a new one on Enter), and burned after five wrong guesses. Pairing yields a per-pairing secret that is returned once. The browser stores it as a **non-extractable WebCrypto key in IndexedDB**, so page scripts can use it to sign but cannot read it out. A pairing is bound to the page's origin.
* **Signed requests with replay protection.** Every other request carries `ts`, `nonce` and an HMAC of method, path, timestamp, nonce and body under the pairing secret. A timestamp more than a minute off, a repeated nonce, a wrong key or a body that changed is refused.
* **Origin and Host checks.** The request's `Origin` must be the paired origin; the `Host` header must be the loopback address and port, which defeats DNS rebinding (a page whose name was re-pointed at 127.0.0.1 is refused).
* **No access log.** Nothing about prompts or addresses is written by the companion.
* **Bounded.** Request bodies are limited to 1 MB and a prompt to 200,000 characters; a model is downloaded only when the paired browser asks and, unless `--allow-custom-models` is given, only if it is in the companion's list.
* **Cancellation is real.** Cancelling shuts the socket to the runtime, which stops generation (tested: the runtime sees the connection close and the companion frees its slot).

The server side adds its own checks (`hosted/service.py`): the prepared request id is unguessable, single-use, bound to the business **and** the person, expires, and the reply is verified claim by claim against the evidence held for it — a companion cannot make the service accept an unsupported claim, and a compromised page cannot ask the service to accept an answer for evidence it never issued.

## "Local" is never claimed when something else happened

The answer's `execution.mode` is `local` only for a reply that came back through `local_complete` from the paired companion. If the companion is missing, the router refuses (`local:no_companion`); if the companion fails, the failure is reported as `provider:<kind>` with the local runtime named, and the business's `fallback` setting decides between the deterministic answer and "unavailable". There is no route from local mode to a cloud provider.

## Tests

`tests/test_companion.py` (53): hardware parsers on recorded output, fit verdicts (a 3B model on 8 GB is not "comfortable"), pairing (one-use, expiry, origin binding), signing and replay, DNS-rebinding refusal, loopback-only runtimes, streaming with true cancellation, size limits; `tests/test_ai_modes.py`: the two-phase flow with the evidence kept on the server, one-use request ids, the wrong person/business, expiry, and that no provider is called in local mode; `web/tests/companion.test.js`: pairing, signing, status and recommendation in the page.
