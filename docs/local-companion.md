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

## Customer installation (macOS, Apple silicon): no Terminal

**Experience.** On the Knovent site a customer chooses "On my computer". The page asks once whether it may look for the AI on their computer (their browser may add its own "allow local network" question). If the companion is not there it shows **Download Knovent for Mac**. They open the downloaded *Knovent Companion*; a normal macOS dialog explains what will happen and asks to continue. The installer copies the companion to `~/Library/Application Support/Knovent`, registers a per-user LaunchAgent (starts at every login, restarted if it stops), checks that it answers, and looks for Ollama. If Ollama is missing it asks, downloads it from ollama.com over https, has macOS check its signature, and puts it in `~/Applications` (no password). The page finds the companion by itself (it checks every few seconds), pairs without a code (the build trusts only the site address it was built for), shows which models fit, uses an installed model without downloading anything, or downloads the recommended one only after the customer approves it, with progress and a Cancel. It then shows **Local AI ready**.

**Parts (one implementation each).** `companion/installer.py` (the steps: copy, LaunchAgent, health check, runtime, uninstall; every question goes through a `UI`), `companion/macui.py` (the questions as macOS dialogs), `companion/app_main.py` (the program: double-click = setup, login service = run), `companion/lifecycle.py` (`OllamaSupervisor`: finds Ollama, starts it only when installed and not running, never a second copy, watches and restarts it with a limit, stops only what it started), `companion/server.py` (model download de-duplicated per model and cancellable at `/v1/models/pull/cancel`; provisioning starts the runtime and says plainly when it is not installed), `scripts/build_companion_macos.sh` (builds `Knovent Companion.app` with PyInstaller; the site address is written into the build). `COMPANION_DOWNLOAD_URL` (Vercel env, https only) is the address the site's button points to (for example a GitHub release).

**Security.** The companion binds 127.0.0.1 only. Pairing keeps its signed requests; a site may pair without a code only if its address was built in (or given with `--origin`), and a browser sets `Origin` itself. Every request is validated; the companion exposes models, status, download and inference only: no file access, no shell. Nothing is downloaded or installed without a question the person answers. Local-only answering is unchanged; no hosted fallback is enabled by installation.

**Supported systems.** macOS on Apple silicon: built, and the install, restart and uninstall below were run on a real Apple M2 Mac (macOS 15.2). **Not implemented:** Windows (plan: a per-user Task Scheduler "at logon" task or a Run key, the same companion built with PyInstaller, a signed MSI/EXE, Ollama for Windows installed with consent) and Linux (plan: a systemd user service, an AppImage or .deb, Ollama's own installer with consent). The site says plainly that they are not supported.

**Tested on a real machine (2026-11-03, test label and port, nothing of the real login items touched).** Built the program (4 MB, no Python needed); `setup` registered the LaunchAgent and the health check passed; the build's site address paired without a code and another address was refused; killing the companion restarted it in 1 s; killing Ollama made the installed companion start it again (1 to 3 s, models intact, the companion as its parent); `uninstall` removed the service and files and left Ollama and the models. **Not tested:** the double-click dialogs (they need a person at the screen), the Ollama download and install path (Ollama was already installed; covered by unit tests with a stand-in download), a real model download and cancel (a 1 GB+ download; covered by unit tests), Macs with Intel chips, a clean Mac without Ollama, Gatekeeper's first-open flow on a fresh download.

**Limits.** The build is not signed with an Apple Developer ID or notarized: macOS asks the customer to open it once with Control-click > Open, and a signed, notarized build is needed before it is offered widely. Safari cannot reach a program on the computer from a secure website: customers there use Chrome, Edge or Brave. The hosted site still needs the engine service to answer questions.

### Pairing contract (automatic, bound to a business)

* A page served from the site address the companion was built for (or started with `--origin`) pairs **without a code**: `POST /v1/pair {label, business}`. A browser sets `Origin` itself; any other address gets `401 {"error":"origin_not_trusted"}` (with CORS headers, so the page can read the reason). A wrong code gets `401 {"error":"code_not_accepted"}`. Manual pairing with the code shown on the companion stays as the fallback.
* A key belongs to **one business** in one browser address: the page sends `X-Companion-Business` and signs it; the companion refuses the key for any other business. Pairing a second business never touches the first. A key expires after 90 days; a revoked, expired or forgotten key is replaced by the page itself, once, when the address is trusted.
* Every refusal the browser must act on carries the CORS headers (only for a well formed `Origin` and `Host`). Without them Chrome hides the 401 and a page wrongly reports a companion that answered as "not reachable". Signed-request refusals keep the same words for every cause.
* Keys made before this change (no business) keep working until they are replaced.

## macOS distribution build (onedir app, optional signing and notarization)

`scripts/build_companion_macos.sh https://your-site.vercel.app` builds `Knovent Companion.app` as a **onedir** bundle (libraries inside the bundle, nothing unpacked at run time; PyInstaller's supported form for a macOS app) and `dist/Knovent-Companion-<version>-macOS-<arch>.zip` plus a `.sha256`. The ZIP is made with `ditto --norsrc --noextattr --noqtn`, so it contains no AppleDouble `._` files, `__MACOSX` or `.DS_Store`. Files are signed inside out (every Mach-O file, then the bundle; never `--deep`).

* **Development build (default):** ad-hoc signed. It runs on the machine that built it; other Macs refuse it until it is Developer ID signed and notarized.
* **Developer ID signing:** `KNOVENT_SIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)"`, an identity that is already in your login keychain (`security find-identity -v -p codesigning`). Adds the hardened runtime and a secure timestamp, with the entitlements in `scripts/macos/entitlements.plist` (none granted). No identity, password or key is stored in the repository or the script.
* **Notarization and stapling:** `KNOVENT_NOTARY_PROFILE=<profile>` created once with `xcrun notarytool store-credentials` (the credentials stay in your keychain). The script submits with `notarytool submit --wait`, staples only if Apple's status is `Accepted`, rebuilds the ZIP with the stapled app, and otherwise prints that it was NOT notarized and how to read Apple's log.
* **Verification:** `scripts/verify_companion_macos.sh "<app>" [--release]` prints PASS, FAIL or NOT RUN for: bundle structure, identifier, version, onedir layout, one architecture across every Mach-O file, no library loaded from outside the bundle or the system, no build-machine path, signature validity, Developer ID / team / hardened runtime / timestamp, Gatekeeper, stapled ticket, ZIP hygiene and checksum. `--release` turns the Apple-account checks from NOT RUN into required.
* **Installing:** the installer copies the whole bundle with `ditto` (signature intact) to `~/Library/Application Support/Knovent` and points the LaunchAgent into it; an upgrade replaces the bundle.

**Run on 2026-11-03 (Apple M2, macOS 15.2, ad-hoc build):** all structure, architecture (45 Mach-O files, all arm64), dependency, signature-validity and ZIP checks passed. NOT RUN, because they require an Apple Developer account and this Mac's keychain holds no Developer ID identity: Developer ID signature, hardened runtime, secure timestamp, Gatekeeper acceptance, notarization and stapling. The Developer ID and notarization code paths in the build script have not been executed. Install, pairing (trusted site paired, another site refused), launchd restart after a crash, Ollama recovery with no duplicate process, and uninstall were run from the unzipped ZIP.


### Zero-configuration install, origins and lifecycle

Nobody types a command, an origin or a pairing code. What makes that safe:

- **Trusted website addresses live in one validated file**, `trusted_origins.json` in the companion's config folder (`{schema, production[], development[]}`, mode 0600). The installer writes it from the build's own `build_config.json` **before the service starts**; it survives reboots and upgrades. Production accepts exact `https://host` names only (no wildcards, paths, ports or userinfo); `http://localhost`/`127.0.0.1`/`*.localhost` are accepted only in the separate `development` list and only by a build made with `KNOVENT_CHANNEL=development`. A damaged or unknown file trusts nothing. The companion re-reads it when it changes, so adding or removing an address needs no restart. Command-line `--origin` values are session-only and never saved.
- **One companion per port.** A lock file (`companion-<port>.lock`) names the running process; a second start prints "already running" and exits. Only the installed service (started with `--replace`) takes over, and only from a process whose command line is Knovent's. Upgrades replace the old companion themselves; nothing is `pkill`ed by hand.
- **Ollama is found, started and watched** by the companion (never a second copy). When it crashes, model runner processes it leaves behind are cleared before the next server starts, so a small computer is not filled with orphaned copies of a model. If Ollama is missing, the installer installs it under the same single approval it asks for up front; models are still downloaded only after the person approves them on the website.
- **Pairing is automatic after explicit consent** on the website ("Allow and check my computer"). The page discovers the companion (`/v1/hello`), then asks it to pair; the companion refuses origins it does not trust (`origin_not_trusted`, readable by the page). The key is bound to one business and one browser origin, lasts 90 days, and a business can be unpaired at any time (the page then offers one click to authorize again; it does not re-pair silently).

Verified on a real Mac (arm64): `scripts/platform_selftest.py` checks 1-10, 13-24 pass; the website flow was driven in real Chrome through consent, automatic pairing, a revoked key (re-paired by itself), unpair and re-authorize, an untrusted address, and a stopped companion. A reboot, logout/login and the Gatekeeper first-open need a person at the machine and are not yet verified. Windows and Linux are not supported until tested on real machines.
