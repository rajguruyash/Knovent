# Knovent on one computer: `faqengine app`

One command (or double-click `Start Knovent.command`) starts everything: the website, the real engine, the local-model companion and Ollama (if installed). Open http://127.0.0.1:8768.

* **Real:** files are read by the same reader as the hosted service, embedded with the same MiniLM (int8 ONNX), searched with the same hybrid index, answered with the same evidence checks and the fast path (an exact answer asks no model). Missing knowledge stays an honest "I don't know". Files and settings are kept in `~/.knovent`.
* **Local model:** the website pairs with the companion by itself (it trusts pages served from this computer), selects an installed model or downloads the recommended one on one click, and answers that need a model go to Ollama on this computer, with every sentence checked against the evidence (a failed check falls back to the deterministic answer). **Not yet run against a real model through this app.**
* **Not real here:** accounts are a local stand-in (any email and password; one person, this computer), consent/usage screens are stand-ins, web-address sources are not read, and **MiniLM is not fine-tuned**: files are embedded with the base model. Fine-tuning exists in the Studio (`faqengine studio`).
* **Hosting (GitHub + Vercel + Supabase):** `web/` deploys to Vercel and talks to Supabase; the hosted engine (`src/faqengine/hosted`, `deploy/engine/Dockerfile`) is a separate Python service that needs its own host and the Supabase migrations (`supabase/migrations`). This app is the local twin of that, not a replacement.
