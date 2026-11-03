"""A lightweight benchmark of language models as the reasoning step over EVIDENCE THE ENGINE ALREADY RETRIEVED. Not production code: it exists to choose which models are worth a production integration.

    python scripts/llm_bench.py capture --data examples/reasoning --set dev --out runs/llm/evidence.reasoning.dev.json     # freeze what retrieval found (no model, no network)
    python scripts/llm_bench.py run --evidence runs/llm/evidence.reasoning.dev.json --deterministic                         # the engine as it is, scored the same way
    python scripts/llm_bench.py run --evidence ... --provider ollama --model ministral-3:3b
    CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=... python scripts/llm_bench.py run --evidence ... --provider cloudflare --model @cf/google/gemma-4-26b-a4b-it --price-in 0.10 --price-out 0.30
    OPENROUTER_API_KEY=... python scripts/llm_bench.py run --evidence ... --provider openrouter --model <id>
    OPENAI_API_KEY=... python scripts/llm_bench.py run --evidence ... --provider openai --model <id>        # also anthropic (ANTHROPIC_API_KEY), gemini (GEMINI_API_KEY)

Hosted providers are called through the PRODUCTION adapters (faqengine.inference: the same request building, parameter negotiation, size caps, no-redirect and public-address rules and failure
classification the service uses), so what is measured is the code that will make the calls. The benchmark's credentials are the CLOUDFLARE_* / OPENROUTER_* / OPENAI_* ... names above; they are
deliberately not the PLATFORM_* names the service reads. Ollama is called through its own native route (it reports load and prompt time, which the production adapter does not need to).

The sealed final holdout (`holdout`): captured only with --allow-sealed, run only with --final, reports aggregate numbers only, and every run is appended to runs/llm/holdout.ledger.jsonl so how
many times it was looked at is on record. It is meant to be run ONCE, for the model chosen on the other sets.

The minimal provider interface is `Provider.complete(system, user, schema) -> Reply` (below). Every model sees IDENTICAL frozen evidence (immutable ids E1..En), answers in one JSON object, and is
scored with the same judge as the engine (scripts/unified_benchmark.py), plus what only a model can get wrong: ids it invented, numbers that are in no cited evidence, invalid JSON. A failure of the
PROVIDER (rate limit, timeout, server error, bad credentials) is counted apart from a failure of the MODEL (bad JSON, invented id, wrong answer) and never lowers the model's accuracy.
Keys come from environment variables only and are never written to a result file. The locked set is refused.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

SEALED = ("locked", "verify", "holdout")
SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": ["answered", "partial", "conflict", "insufficient"]},
        "missing": {"type": "array", "items": {"type": "string"}},
        "answer": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object", "properties": {"text": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                                              "required": ["text", "evidence"]}},
    },
    "required": ["requirements", "verdict", "missing", "answer", "claims"],
}
SYSTEM = ("You answer a business question using ONLY the numbered evidence. Evidence ids look like E1, E2. Never use any other id and never use knowledge that is not in the evidence. "
          "Respond with one JSON object: requirements (what the question needs, as short phrases), verdict (answered | partial | conflict | insufficient), missing (requirements the evidence does not "
          "support), answer (plain text; every sentence ends with its evidence ids in brackets like [E2]; empty when verdict is insufficient), claims (each factual claim with the evidence ids that "
          "support it). Say conflict when two evidence items disagree about the same thing and do not choose between them. Say partial when only some requirements are supported. A statement about a "
          "related thing is not support (an annual BILLING option is not an annual DISCOUNT). Copy numbers exactly as written; do not calculate.")


# ---------- the minimal provider interface

@dataclass
class Reply:
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    ttft: float | None = None  # seconds until the first token (model load + reading the prompt), where the runtime reports it
    load: float | None = None  # seconds spent loading the model into memory for this request
    failure: str | None = None  # "provider:<kind>" (rate_limited, server, timeout, network, auth, not_found) — never the model's fault


class Provider:
    name = "base"

    def complete(self, system: str, user: str, schema: dict | None = None) -> Reply:  # pragma: no cover - interface
        raise NotImplementedError


class OpenAICompatible(Provider):
    """Ollama (http://localhost:11434/v1), Cloudflare Workers AI, OpenRouter and any other server that speaks /chat/completions."""

    def __init__(self, name: str, base_url: str, model: str, key: str = "", timeout: float = 120.0, mode: str = "schema", extra: dict | None = None):
        self.name, self.base, self.model, self.key, self.timeout, self.mode, self.extra = name, base_url.rstrip("/"), model, key, timeout, mode, extra or {}

    def complete(self, system: str, user: str, schema: dict | None = None) -> Reply:
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0, "max_tokens": 1500, **self.extra}
        if schema and self.mode == "schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        elif schema and self.mode == "json_object":
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        started = time.perf_counter()
        try:
            req = urllib.request.Request(f"{self.base}/chat/completions", data=json.dumps(body).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            kind = "rate_limited" if e.code == 429 else "server" if e.code >= 500 else "auth" if e.code in (401, 403) else "not_found" if e.code == 404 else f"bad_request_{e.code}"
            return Reply(failure=f"provider:{kind}", seconds=time.perf_counter() - started)
        except TimeoutError:
            return Reply(failure="provider:timeout", seconds=time.perf_counter() - started)
        except (urllib.error.URLError, ConnectionError, OSError):
            return Reply(failure="provider:network", seconds=time.perf_counter() - started)
        except ValueError:
            return Reply(failure="provider:server", seconds=time.perf_counter() - started)
        usage = data.get("usage") or {}
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            return Reply(failure="provider:server", seconds=time.perf_counter() - started)
        return Reply(text=text, tokens_in=int(usage.get("prompt_tokens") or 0), tokens_out=int(usage.get("completion_tokens") or 0), seconds=time.perf_counter() - started)


class OllamaNative(Provider):
    """Ollama's own /api/chat: it reports model load time, prompt-reading time and generation time, which the OpenAI-compatible route hides. Thinking is switched off (an answer is wanted, not a monologue)."""
    name = "ollama"

    def __init__(self, model: str, base: str = "http://localhost:11434", timeout: float = 300.0, schema_mode: bool = True, num_ctx: int = 8192):
        self.model, self.base, self.timeout, self.schema_mode, self.num_ctx = model, base.rstrip("/"), timeout, schema_mode, num_ctx

    def complete(self, system: str, user: str, schema: dict | None = None) -> Reply:
        body = {"model": self.model, "stream": False, "think": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": 0, "num_ctx": self.num_ctx, "num_predict": 1200}}
        if schema and self.schema_mode:
            body["format"] = schema
        started = time.perf_counter()
        try:
            req = urllib.request.Request(f"{self.base}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            return Reply(failure="provider:not_found" if e.code == 404 else "provider:server", seconds=time.perf_counter() - started)
        except TimeoutError:
            return Reply(failure="provider:timeout", seconds=time.perf_counter() - started)
        except (urllib.error.URLError, ConnectionError, OSError, ValueError):
            return Reply(failure="provider:network", seconds=time.perf_counter() - started)
        ns = lambda k: (data.get(k) or 0) / 1e9  # noqa: E731
        return Reply(text=(data.get("message") or {}).get("content", ""), tokens_in=int(data.get("prompt_eval_count") or 0), tokens_out=int(data.get("eval_count") or 0),
                     seconds=ns("total_duration") or (time.perf_counter() - started), ttft=ns("load_duration") + ns("prompt_eval_duration"), load=ns("load_duration"))


class Production(Provider):
    """A hosted or local provider through the production adapter for it (faqengine.inference.registry). Failures keep the production vocabulary ("provider:<kind>")."""

    STRUCTURED = {"schema": "json_schema", "json_object": "json_object", "none": "prompt"}

    def __init__(self, provider: str, model: str, mode: str, key: str = "", base_url: str = "", platform=None, timeout: float = 120.0, structured: str = "schema", extra: dict | None = None):
        from faqengine.inference import registry

        self.name = provider
        self.adapter = registry.build(provider, model, mode, key=key, base_url=base_url, platform=platform, timeout=timeout, structured=self.STRUCTURED[structured], max_tokens=1500,
                                      allow_private=mode == "local")
        self.adapter.extra_body.update(extra or {})  # (on top of what the catalogue records for the model)

    def complete(self, system: str, user: str, schema: dict | None = None) -> Reply:
        r = self.adapter.complete(system, user, schema)
        return Reply(text=r.text or "", tokens_in=r.tokens_in, tokens_out=r.tokens_out, seconds=r.seconds, failure=r.failure)


class MemorySampler:
    """Peak resident memory of the model runtime's processes (every process whose command contains "ollama"), sampled while the benchmark runs."""

    def __init__(self, needle: str = "ollama"):
        import threading

        self.needle, self.peak, self._stop = needle, 0, threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        import subprocess

        while not self._stop.is_set():
            out = subprocess.run(["ps", "-axo", "rss=,command="], capture_output=True, text=True).stdout
            total = sum(int(line.split(None, 1)[0]) for line in out.splitlines() if self.needle in line and "llm_bench" not in line and line.strip()[:1].isdigit())
            self.peak = max(self.peak, total)
            self._stop.wait(0.5)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=2)


def make_provider(args) -> Provider:
    extra = json.loads(args.extra) if args.extra else {}
    if args.provider == "ollama":
        return OllamaNative(args.model, (args.base_url or "http://localhost:11434").replace("/v1", ""), args.timeout, args.mode == "schema")
    from faqengine.inference import registry

    def need(name: str) -> str:
        v = os.environ.get(name, "")
        if not v:
            sys.exit(f"set {name} in the environment (never on the command line)")
        return v
    if args.provider == "cloudflare":
        creds = registry.PlatformCredentials((("PLATFORM_CLOUDFLARE_ACCOUNT_ID", need("CLOUDFLARE_ACCOUNT_ID")), ("PLATFORM_CLOUDFLARE_API_TOKEN", need("CLOUDFLARE_API_TOKEN"))))
        return Production("cloudflare", args.model, "platform", platform=creds, timeout=args.timeout, structured=args.mode, extra=extra)
    if args.provider == "openrouter":
        creds = registry.PlatformCredentials((("PLATFORM_OPENROUTER_API_KEY", need("OPENROUTER_API_KEY")),))
        return Production("openrouter", args.model, "platform", platform=creds, timeout=args.timeout, structured=args.mode, extra=extra)
    if args.provider in ("openai", "anthropic", "gemini"):
        return Production(args.provider, args.model, "byok", key=need({"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}[args.provider]), timeout=args.timeout, structured=args.mode, extra=extra)
    if args.provider == "custom":
        return Production("openai_compatible", args.model, "byok", key=os.environ.get("LLM_API_KEY", ""), base_url=args.base_url or "", timeout=args.timeout, structured=args.mode, extra=extra)
    sys.exit(f"unknown provider {args.provider}")


# ---------- freezing the evidence

def capture(args) -> None:
    import csv

    import unified_benchmark as UB
    from faqengine.runtime import answer, evidence
    from faqengine.runtime.embedder import Embedder
    from datetime import date

    if args.set in SEALED and not args.allow_sealed:
        sys.exit("the locked, verify and holdout sets are not captured for model comparison")
    UB.DATA = (ROOT / args.data).resolve()
    rows = [dict(r) for r in csv.DictReader(open(UB.DATA / f"{args.set}.csv", encoding="utf-8"))]
    emb = Embedder(ROOT / "hosted_models" / "minilm-l6" / "model")
    kb, index, _ = UB.build(emb)
    sim = lambda part, texts: (emb.embed_docs_cached(texts) @ emb.embed([part], "query")[0]).tolist() if texts else []  # noqa: E731

    def search(q, on=None):
        f, p, d = index.search(q, emb.embed([q], "query")[0], **({"on": on} if on is not None else {}))
        d["embed_ms"] = d["total_ms"] = 0.0
        return f, p, d

    search.model = index.fact_model
    out = []
    for row in rows:
        r = answer.respond(row["question"], search, sim, None, 0.4, evidence.SEM_MIN, as_of=date.fromisoformat(UB.AS_OF))
        items, seen = [], set()
        for c in (r.get("retrieval") or {}).get("candidates", []):
            e = next((x for x in index.entries if x["id"] == c["entry_id"]), None)
            if e and e["id"] not in seen:
                seen.add(e["id"])
                items.append({"entry_id": e["id"], "source": e.get("source", ""), "heading": e.get("question", ""), "text": e.get("answer", "")[:900], "kind": e.get("kind", "passage")})
        for c in list(items):  # the other rows of a table that retrieval touched: "which plan is cheapest" is about every row (the same rows the deterministic layer is given)
            e = next((x for x in index.entries if x["id"] == c["entry_id"]), None)
            if e and e.get("record"):
                table = (e["source"], e["record"].get("table"))
                for x in index.entries:
                    if x.get("record") and (x["source"], x["record"].get("table")) == table and x["id"] not in seen and len(items) < 16 and not x.get("inactive"):
                        seen.add(x["id"])
                        items.append({"entry_id": x["id"], "source": x["source"], "heading": x["question"], "text": x["answer"][:900], "kind": "passage"})
        for n, it in enumerate(items, 1):
            it["id"] = f"E{n}"
        out.append({"row": row, "evidence": items, "deterministic": {"outcome": r["outcome"], "answer": r.get("answer") or r.get("partial_answer") or r.get("message") or "",
                                                                   "sources": r.get("sources") or [], "conflicts": r.get("conflicts") or [], "candidates": (r.get("retrieval") or {}).get("candidates", [])}})
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"data": args.data, "set": args.set, "as_of": UB.AS_OF, "items": out}, ensure_ascii=False, indent=1))
    print(f"froze {len(out)} questions with {sum(len(o['evidence']) for o in out)} evidence items -> {args.out}")


# ---------- asking a model, and judging what it said

def prompt(item: dict) -> str:
    ev = "\n".join(f"[{e['id']}] ({e['source']} › {e['heading']}) {e['text']}" for e in item["evidence"])
    return f"Question: {item['row']['question']}\n\nEvidence:\n{ev or '(none)'}"


def parse_json(text: str):
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def judge_model(item: dict, reply: Reply) -> dict:
    """What the model said, as a result the engine's own judge understands, plus the checks only a model needs."""
    import unified_benchmark as UB

    by_id = {e["id"]: e for e in item["evidence"]}
    data = parse_json(reply.text)
    base = {"valid_json": False, "invented_ids": 0, "uncited_claims": 0, "ungrounded_numbers": 0, "schema_ok": False}
    if not isinstance(data, dict):
        r = {"outcome": "insufficient", "answer": None, "message": "", "sources": [], "retrieval": {"candidates": []}}
        return {**base, **UB.judge(item["row"], r), "model_failure": "invalid_json"}
    base["valid_json"] = True
    base["schema_ok"] = all(k in data for k in SCHEMA["required"]) and data.get("verdict") in SCHEMA["properties"]["verdict"]["enum"]
    claims = [c for c in (data.get("claims") or []) if isinstance(c, dict)]
    cited = [i for c in claims for i in (c.get("evidence") or []) if isinstance(i, str)] + re.findall(r"\bE\d+\b", str(data.get("answer") or ""))
    base["invented_ids"] = len({i for i in cited if i not in by_id})
    base["uncited_claims"] = sum(1 for c in claims if not [i for i in (c.get("evidence") or []) if i in by_id])
    valid = list(dict.fromkeys(i for i in cited if i in by_id))
    text = str(data.get("answer") or "")
    cited_text = " ".join(by_id[i]["text"] for i in valid)
    base["ungrounded_numbers"] = sum(1 for n in set(NUM.findall(re.sub(r"\[?\bE\d+\b\]?", " ", text))) if n.replace(",", "") not in cited_text.replace(",", ""))
    verdict = data.get("verdict")
    outcome = {"answered": "grounded", "partial": "partial", "conflict": "conflict", "insufficient": "insufficient"}.get(verdict, "insufficient")
    srcs = [{"source": by_id[i]["source"], "location": f"{by_id[i]['source']} › {by_id[i]['heading']}", "id": by_id[i]["entry_id"]} for i in valid]
    r = {"outcome": outcome, "answer": text if outcome == "grounded" else None, "partial_answer": text if outcome == "partial" else None,
         "message": text if outcome in ("insufficient", "conflict") else "", "sources": srcs,
         "conflicts": [{"sources": srcs}] if outcome == "conflict" else [], "retrieval": {"candidates": [{"source": e["source"]} for e in item["evidence"]]}}
    return {**base, **UB.judge(item["row"], r), "model_failure": None}


def judge_deterministic(item: dict) -> dict:
    import unified_benchmark as UB

    d = item["deterministic"]
    r = {"outcome": d["outcome"], "answer": d["answer"] if d["outcome"] in ("faq", "grounded") else None, "partial_answer": d["answer"] if d["outcome"] == "partial" else None,
         "message": d["answer"] if d["outcome"] not in ("faq", "grounded", "partial") else "", "sources": d["sources"], "conflicts": d["conflicts"], "retrieval": {"candidates": d["candidates"]}}
    return {"valid_json": True, "invented_ids": 0, "uncited_claims": 0, "ungrounded_numbers": 0, "schema_ok": True, "model_failure": None, **UB.judge(item["row"], r)}


def run(args) -> None:
    import unified_benchmark as UB

    frozen = json.loads(Path(args.evidence).read_text())
    if frozen["set"] in ("locked", "verify"):
        sys.exit("refusing to compare models on a sealed set")
    if frozen["set"] == "holdout" and not args.final:
        sys.exit("the final holdout is run once, for the model already chosen on the other sets: pass --final (every run is recorded in runs/llm/holdout.ledger.jsonl)")
    if frozen["set"] != "holdout" and args.final:
        sys.exit("--final is only for the holdout set")
    items = frozen["items"][: args.limit] if args.limit else frozen["items"]
    provider = None if args.deterministic else make_provider(args)
    judged, rows, latencies, tin, tout, provider_failures, ttfts, loads = [], [], [], 0, 0, {}, [], []
    sampler = MemorySampler() if (provider is not None and provider.name == "ollama") else None
    if sampler:
        sampler.__enter__()
    done_file = Path(args.json + ".partial.jsonl") if args.json and provider is not None and frozen["set"] != "holdout" else None  # (no per-question record of the holdout is kept)  # every answered question is saved at once: a run that is stopped keeps what it did
    saved = {}
    if done_file and done_file.exists() and args.resume:
        saved = {d["id"]: d for d in map(json.loads, done_file.read_text().splitlines() if done_file.read_text().strip() else [])}
    for it in items:
        if it["row"]["id"] in saved:
            d = saved[it["row"]["id"]]
            judged.append(d["judged"])
            rows.append(it["row"])
            latencies.append(d["seconds"])
            tin, tout = tin + d["tin"], tout + d["tout"]
            if d.get("ttft") is not None:
                ttfts.append(d["ttft"])
                loads.append(d.get("load") or 0.0)
            continue
        if provider is None:
            j = judge_deterministic(it)
        else:
            reply = None
            for attempt in range(args.retries + 1):  # bounded retries, for the PROVIDER's failures only
                reply = provider.complete(SYSTEM, prompt(it), SCHEMA)
                if not reply.failure or reply.failure.split(":")[1] not in ("rate_limited", "server", "timeout", "network"):
                    break
                time.sleep(min(2 ** attempt, 8))
            if reply.failure:
                provider_failures[reply.failure] = provider_failures.get(reply.failure, 0) + 1
                continue  # not the model's fault: left out of its accuracy, counted
            latencies.append(reply.seconds)
            if reply.ttft is not None:
                ttfts.append(reply.ttft)
                loads.append(reply.load or 0.0)
            tin, tout = tin + reply.tokens_in, tout + reply.tokens_out
            j = judge_model(it, reply)
            if done_file:
                with done_file.open("a") as f:
                    f.write(json.dumps({"id": it["row"]["id"], "judged": j, "seconds": reply.seconds, "tin": reply.tokens_in, "tout": reply.tokens_out, "ttft": reply.ttft, "load": reply.load}) + "\n")
        judged.append(j)
        rows.append(it["row"])
    if sampler:
        sampler.__exit__()
    summary = UB.summarise(rows, judged) if rows else {}
    n = max(len(judged), 1)
    cost = None if args.price_in is None or args.price_out is None else round((tin * args.price_in + tout * args.price_out) / 1e6, 5)
    summary.update({
        "label": "deterministic" if provider is None else f"{args.provider}:{args.model}", "asked": len(items), "answered_by_provider": len(judged), "provider_failures": provider_failures,
        "invalid_json": sum(bool(j["model_failure"]) for j in judged), "schema_ok": sum(j["schema_ok"] for j in judged), "invented_evidence_ids": sum(j["invented_ids"] for j in judged),
        "claims_without_valid_evidence": sum(j["uncited_claims"] for j in judged), "numbers_not_in_cited_evidence": sum(j["ungrounded_numbers"] for j in judged),
        "median_latency_s": round(sorted(latencies)[len(latencies) // 2], 2) if latencies else None, "p90_latency_s": round(sorted(latencies)[int(len(latencies) * 0.9)], 2) if latencies else None,
        "median_time_to_first_token_s": round(sorted(ttfts)[len(ttfts) // 2], 2) if ttfts else None, "max_model_load_s": round(max(loads), 2) if loads else None,
        "peak_runtime_memory_mb": round(sampler.peak / 1024) if sampler else None, "tokens_in": tin, "tokens_out": tout,
        "tokens_per_question": round((tin + tout) / n) if provider else 0, "measured_cost_usd": cost,
        "cost_per_1000_questions_usd": None if cost is None else round(cost / n * 1000, 3)})
    summary["metrics"] = seventeen(summary, judged, rows, latencies, len(items), provider_failures, cost, n)
    if frozen["set"] == "holdout":
        summary.pop("by_category_correct", None), summary.pop("by_category_citation", None), summary.pop("by_source_type_correct", None)  # aggregate only: no breakdown fine enough to read questions off
        ledger = ROOT / "runs" / "llm" / "holdout.ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "label": summary["label"], "evidence": str(args.evidence), "questions": len(items), "answered_by_provider": len(judged)}) + "\n")
    print(json.dumps(summary, indent=1))
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(summary, indent=1))


def seventeen(summary: dict, judged: list[dict], rows: list[dict], latencies: list[float], asked: int, provider_failures: dict, cost, n: int) -> dict:
    """The seventeen measures the model choice is made on, each computed from this run alone (None where nothing could be measured). Ordered so that the ones that matter most for trust come first
    in the report; a model is not chosen by accuracy alone."""
    def share(pred, ids):
        ids = list(ids)
        return None if not ids else round(sum(bool(pred(i)) for i in ids) / len(ids), 3)
    by = lambda *cats: [i for i, r in enumerate(rows) if r["category"] in cats]  # noqa: E731
    pcts = sorted(latencies)
    answered_ids = [i for i, j in enumerate(judged) if j["valid_json"]]
    provider_fail = sum(provider_failures.values())
    return {
        "1_overall_correctness": summary.get("correct_answer_rate"),
        "2_false_answer_rate": summary.get("false_answer_rate"),
        "3_citation_correctness": summary.get("citation_correct"),
        "4_abstention_precision": summary.get("abstention_precision"),
        "5_abstention_recall": summary.get("abstention_recall"),
        "6_unnecessary_abstention": summary.get("unnecessary_abstention_rate"),
        "7_conflict_detection": summary.get("conflict_detected"),
        "8_missing_information_handling": share(lambda i: judged[i]["correct"], by("missing", "missing_value", "unsupported", "partial")),
        "9_multi_source_reasoning": share(lambda i: judged[i]["correct"], by("multi_source", "multi_condition", "multi_part", "multi_sentence")),
        "10_structured_reasoning": share(lambda i: judged[i]["correct"], by("structured", "comparison", "price_calc", "limit_check", "feature_compare", "semantic_header")),
        "11_invalid_json_rate": None if not judged else round(sum(bool(j["model_failure"]) for j in judged) / len(judged), 3),
        "12_fabricated_evidence_id_rate": None if not answered_ids else round(sum(judged[i]["invented_ids"] > 0 for i in answered_ids) / len(answered_ids), 3),
        "13_provider_failure_rate": None if not asked else round(provider_fail / asked, 3),
        "14_median_latency_s": round(pcts[len(pcts) // 2], 2) if pcts else None,
        "15_p95_latency_s": round(pcts[min(len(pcts) - 1, int(len(pcts) * 0.95))], 2) if pcts else None,
        "16_cost_per_question_usd": None if cost is None or not n else round(cost / n, 6),
        "17_cost_per_1000_questions_usd": None if cost is None or not n else round(cost / n * 1000, 3),
        "note": "categories are the data's own labels; a measure with no questions in its categories is null, not zero. Provider failures are excluded from 1-12 and counted in 13.",
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--data", default="examples/reasoning")
    c.add_argument("--set", default="dev", choices=["dev", "regression", "locked", "verify", "fresh", "holdout"])
    c.add_argument("--out", required=True)
    c.add_argument("--allow-sealed", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("--evidence", required=True)
    r.add_argument("--deterministic", action="store_true")
    r.add_argument("--provider", choices=["ollama", "cloudflare", "openrouter", "openai", "anthropic", "gemini", "custom"])
    r.add_argument("--model")
    r.add_argument("--base-url")
    r.add_argument("--mode", default="schema", choices=["schema", "json_object", "none"], help="how structured output is requested")
    r.add_argument("--extra", help="JSON merged into the request body (for example {\"think\": false})")
    r.add_argument("--timeout", type=float, default=120.0)
    r.add_argument("--retries", type=int, default=2)
    r.add_argument("--limit", type=int)
    r.add_argument("--price-in", type=float, help="USD per million input tokens (from the provider's price page)")
    r.add_argument("--price-out", type=float)
    r.add_argument("--json")
    r.add_argument("--final", action="store_true", help="the one run on the sealed holdout, for the model already chosen on the other sets")
    r.add_argument("--resume", action="store_true", help="continue a stopped run from its .partial.jsonl")
    args = ap.parse_args()
    if args.cmd == "capture":
        capture(args)
    else:
        if not args.deterministic and not (args.provider and args.model):
            sys.exit("give --provider and --model, or --deterministic")
        run(args)


if __name__ == "__main__":
    main()
