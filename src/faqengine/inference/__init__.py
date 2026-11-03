"""The inference layer: how a model is reached, kept apart from what is asked of it (runtime/compose.py, runtime/pipeline.py).

    errors      what can go wrong with a provider, classified once (a provider failure is never a reasoning failure)
    catalogue   which models exist for which provider, what each is verified to support and what it costs (data, not code)
    adapters    one adapter per provider family behind one contract: discovery, inference, structured output, usage, latency, cost, health, cancellation
    resilience  timeouts, bounded retries of the SAME provider and model, a circuit breaker per provider and model: nothing here ever switches to another one
    registry    provider keys, the execution modes each can serve, and how to build an adapter from a stored configuration
    router      the server-side decision of mode, provider, model and credential for one business: consent, verification, limits and security before anything is sent

Standard library only. No module here knows a business's documents; adapters receive text and return text.
"""
