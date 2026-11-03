"""THE LOCAL COMPANION: a small program on the business's OWN computer that lets the hosted application use a model running there. Evidence never goes to an inference provider: the hosted service
hands the prompt to the person's browser (runtime/pipeline.py `prepare`), the browser hands it to the companion on 127.0.0.1, the companion asks the local runtime (Ollama or llama.cpp), and what
comes back is returned to the service, which verifies every claim against the evidence it kept (`conclude_with_reply`).

    hardware    what this machine is (operating system, CPU, memory, GPU) and what it can honestly run
    models      candidate local models (a configurable catalogue) and which of them fit, with the limits said plainly
    runtimes    Ollama and llama.cpp adapters: discovery, models, download, streaming inference with real cancellation
    auth        pairing (a short-lived one-use code), a per-pairing secret, signed requests with replay protection, origin and Host checks
    server      the local HTTP service: bound to loopback only, nothing but the pairing handshake answers without a signature

Standard library only. Nothing here talks to any address but loopback (and, only for a model download the person asks for, an allow-listed host).
"""
