"""Measured performance of the hosted engine on this machine: ingestion throughput, answer latency against knowledge size, concurrency, memory, database connections, and what the model path adds
on top of the deterministic one. A throwaway PostgreSQL with every migration, the real embedding model, the engine called in-process (no HTTP hop: the numbers are the engine's own, a network adds
to them), a stand-in provider that answers instantly (so the model path's OWN overhead is separated from any provider's time).

    .venv/bin/python scripts/perf_measure.py [--sizes 200,1000,3000] [--questions 40] [--out runs/perf/perf.json]

The knowledge is synthetic (distinct codes and values), so the numbers say how fast and how large, NOT how accurate: accuracy is measured on the evaluation sets. Nothing here is a capacity
promise for another machine: the output records this one.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import random
import re
import resource
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fake_provider import FakeProvider  # noqa: E402
from pgdb import make_db  # noqa: E402

from faqengine.hosted import service as svc  # noqa: E402
from faqengine.hosted.secrets import SecretBox  # noqa: E402
from faqengine.hosted.store import Store  # noqa: E402
from faqengine.runtime.embedder import Embedder  # noqa: E402

MODEL = ROOT / "dist" / "bike-shop" / "model"
KEY = "sk-perf-key-0000000000"
NOUNS = ["warranty", "capacity", "weight", "price", "delivery time", "battery life", "range", "noise level", "power draw", "service interval", "load limit", "colour options"]
ADJ = ["standard", "extended", "premium", "compact", "outdoor", "indoor", "heavy duty", "lightweight"]


def synthetic_faq(n: int, seed: int = 7) -> tuple[bytes, list[tuple[str, str]]]:
    rnd = random.Random(seed)
    rows, qa = ["id,question,answer,category,alt_questions"], []
    for i in range(n):
        noun, adj, code = rnd.choice(NOUNS), rnd.choice(ADJ), f"{rnd.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{rnd.randint(100, 999)}-{i}"
        q = f"What is the {noun} of the {adj} model {code}?"
        a = f"The {noun} of the {adj} model {code} is {rnd.randint(2, 900)} units. Ask support if you need a different configuration."
        rows.append(f'faq-{i:05d},"{q}","{a}",catalogue,')
        qa.append((q, a))
    return ("\n".join(rows) + "\n").encode(), qa


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, int(len(s) * q))] * 1000, 1) if s else 0.0


def stats(values: list[float]) -> dict:
    return {"n": len(values), "p50_ms": pct(values, 0.5), "p95_ms": pct(values, 0.95), "max_ms": round(max(values) * 1000, 1) if values else 0.0, "mean_ms": round(statistics.fmean(values) * 1000, 1) if values else 0.0}


def rss_mb() -> float:
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout.strip()
    return round(int(out) / 1024, 1) if out.isdigit() else 0.0


def machine() -> dict:
    info = {"platform": platform.platform(), "machine": platform.machine(), "python": platform.python_version(), "cpus": os.cpu_count()}
    try:
        info["memory_gb"] = round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout.strip()) / 2**30, 1)
    except (ValueError, OSError):
        pass
    return info


class Files(dict):
    def __call__(self, path):
        return self[path]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sizes", default="200,1000,3000")
    ap.add_argument("--questions", type=int, default=40)
    ap.add_argument("--threads", default="1,4,8,16")
    ap.add_argument("--out", default="runs/perf/perf.json")
    args = ap.parse_args()
    sizes, threads = [int(x) for x in args.sizes.split(",")], [int(x) for x in args.threads.split(",")]
    gen = make_db()
    try:
        db = next(gen)
    except BaseException as e:  # a skip outside pytest is an exception
        sys.exit(f"no local PostgreSQL with pgvector to measure on ({type(e).__name__})")
    report: dict = {"machine": machine(), "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sizes": {}, "concurrency": {}, "notes": []}
    peak_conns, live, lock = [0], [0], threading.Lock()
    import psycopg

    real_connect = psycopg.connect

    def counting(*a, **k):
        c = real_connect(*a, **k)
        with lock:
            live[0] += 1
            peak_conns[0] = max(peak_conns[0], live[0])
        close = c.close

        def closer():
            with lock:
                live[0] -= 1
            close()
        c.close = closer
        return c
    psycopg.connect = counting
    try:
        started = time.perf_counter()
        emb = Embedder(MODEL)
        report["embedding_model_load_s"] = round(time.perf_counter() - started, 2)
        report["rss_after_model_load_mb"] = rss_mb()
        box = SecretBox({1: base64.b64decode(SecretBox.generate())})
        provider = FakeProvider(key=KEY, reply=lambda body: json.dumps({"verdict": "answered", "claims": [{"text": (re.search(r"\[E1\] \([^)]*\) (.*)", " ".join(m.get("content", "") for m in body["messages"])) or [None, "x"])[1].split(". ")[0] + ".", "evidence": ["E1"]}], "missing": []}))
        engine = svc.HostedEngine(Store(db.dsn), emb, box, Files(), allow_private_llm=True, rate_per_minute=10**6, ai_modes={"byok"}, inference_per_minute=10**6)
        biggest = None
        for n in sizes:
            owner = str(db.user(f"perf-{n}"))
            biz = str(db.business(owner, f"perf-{n}"))
            data, qa = synthetic_faq(n)
            engine.fetch_object[f"{biz}/faq.csv"] = data
            sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, 'faq.csv', 'faq_table', %s) returning id", (biz, f"{biz}/faq.csv"))[0][0])
            t0 = time.perf_counter()
            done = engine.ingest(biz, owner, sid)
            ingest_s = time.perf_counter() - t0
            entry = {"faqs": done["faqs"], "chunks": done["chunks"], "ingest_s": round(ingest_s, 2), "ingest_faqs_per_s": round(done["faqs"] / ingest_s, 1)}
            t0 = time.perf_counter()
            engine.respond(biz, owner, qa[0][0])  # the first question builds this business's search index in memory
            entry["first_question_after_ingest_s"] = round(time.perf_counter() - t0, 2)
            rnd = random.Random(n)
            picks = [rnd.choice(qa) for _ in range(args.questions)]
            exact, reworded = [], []
            for q, _ in picks:
                t = time.perf_counter()
                r = engine.respond(biz, owner, q)["result"]
                exact.append(time.perf_counter() - t)
            hits = 0
            for q, a in picks:
                t = time.perf_counter()
                r = engine.respond(biz, owner, "Could you tell me " + q[0].lower() + q[1:])["result"]
                reworded.append(time.perf_counter() - t)
                hits += bool(r.get("answer")) and a.split(" is ")[1].split(" units")[0] in (r.get("answer") or "")
            entry.update({"answer_latency_exact_question": stats(exact), "answer_latency_reworded_question": stats(reworded), "reworded_questions_answered_with_the_right_value": f"{hits}/{len(picks)}", "rss_mb": rss_mb()})
            report["sizes"][str(n)] = entry
            print(f"[{n:>5} faqs] ingest {entry['ingest_s']}s ({entry['ingest_faqs_per_s']}/s)  exact p50 {entry['answer_latency_exact_question']['p50_ms']} ms p95 {entry['answer_latency_exact_question']['p95_ms']} ms  rss {entry['rss_mb']} MB", flush=True)
            biggest = (biz, owner, qa)
        biz, owner, qa = biggest
        # ---- concurrency on the largest business
        rnd = random.Random(99)
        for t_count in threads:
            total = max(60, t_count * 15)
            work = [rnd.choice(qa)[0] for _ in range(total)]
            lat, errs, idx = [], [0], [0]
            peak_conns[0] = live[0]

            def worker():
                while True:
                    with lock:
                        i = idx[0]
                        idx[0] += 1
                    if i >= total:
                        return
                    t = time.perf_counter()
                    try:
                        engine.respond(biz, owner, work[i])
                    except Exception:
                        with lock:
                            errs[0] += 1
                        continue
                    with lock:
                        lat.append(time.perf_counter() - t)
            t0 = time.perf_counter()
            pool = [threading.Thread(target=worker) for _ in range(t_count)]
            for th in pool:
                th.start()
            for th in pool:
                th.join()
            wall = time.perf_counter() - t0
            report["concurrency"][str(t_count)] = {"requests": total, "errors": errs[0], "throughput_per_s": round(total / wall, 1), **stats(lat), "peak_db_connections": peak_conns[0], "rss_mb": rss_mb()}
            c = report["concurrency"][str(t_count)]
            print(f"[{t_count:>2} threads] {c['throughput_per_s']} req/s  p50 {c['p50_ms']} ms p95 {c['p95_ms']} ms  errors {c['errors']}  peak db connections {c['peak_db_connections']}", flush=True)
        # ---- what the model path adds, with a provider that answers at once
        db.su("insert into public.llm_provider_configs (business_id, provider, model, base_url, enabled, has_key) values (%s, 'openai_compatible', 'chat-small', %s, true, true)", (biz, provider.url()))
        row = box.encrypt(KEY, biz)
        db.su("insert into public.llm_provider_secrets (business_id, encrypted_api_key, key_version) values (%s, %s, %s)", (biz, row[0], row[1]))
        from faqengine.hosted import disclosure
        from conftest import verified_disclosure_entries  # noqa: E402
        tmp = Path(ROOT / "runs" / "perf" / "disclosure.json")
        tmp.write_text(json.dumps(verified_disclosure_entries()))
        os.environ["AI_DISCLOSURE_FILE"] = str(tmp)
        engine.consent_grant(biz, owner, "openai_compatible", disclosure.current_version("openai_compatible"))
        engine.ai_settings_save(biz, owner, {"mode": "byok"})
        picks = [rnd.choice(qa)[0] for _ in range(args.questions)]
        det, mod, how = [], [], {}
        engine.ai_settings_save(biz, owner, {"mode": "deterministic"})
        for q in picks:
            t = time.perf_counter()
            engine.respond(biz, owner, q)
            det.append(time.perf_counter() - t)
        engine.ai_settings_save(biz, owner, {"mode": "byok"})
        for q in picks:
            t = time.perf_counter()
            r = engine.respond(biz, owner, q)["result"]["execution"]
            mod.append(time.perf_counter() - t)
            key = f"{r.get('generated_by')}" + (f" ({r['failure_kind']})" if r.get("failure_kind") else "")
            how[key] = how.get(key, 0) + 1
        report["model_path_overhead"] = {"deterministic": stats(det), "byok_with_an_instant_provider": stats(mod), "byok_answers_by": how,
                                         "note": "the second line is the pipeline + encryption/consent/limit checks + one local HTTP call to a stand-in; any real provider's time is added to it"}
        print(f"[model path] deterministic p50 {pct(det, .5)} ms; byok (instant stand-in) p50 {pct(mod, .5)} ms; byok answers by {how}", flush=True)
        report["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
        report["notes"] += ["synthetic knowledge: speed and size, not accuracy", "in-process calls: no network hop, no TLS, no Vercel function start", "PostgreSQL is local; a hosted database adds its round-trip time to every query"]
        provider.close()
    finally:
        psycopg.connect = real_connect
        next(gen, None)
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
