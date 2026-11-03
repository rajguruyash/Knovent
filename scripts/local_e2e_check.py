"""THE REAL-DEVICE CHECK for local AI: one business, one real local model, the whole chain, on THIS computer. It is the evidence that "local inference works" needs and that stand-in runtimes cannot give.

    .venv/bin/python scripts/local_e2e_check.py --provision llama3.2-1b --yes        # first time: downloads that ONE model through the companion, after the checks below
    .venv/bin/python scripts/local_e2e_check.py [--model llama3.2:1b] [--questions 4]  # afterwards: the model is already installed

What it does (a throwaway PostgreSQL with every migration, the real embedding model, the companion's real HTTP service on loopback talking to a real Ollama on loopback; every companion call is a
SIGNED request, exactly what the browser sends):
  0. before anything is downloaded: hardware, memory and swap, free disk and the model's download size from Ollama's registry; it stops if the disk would not hold the model with a margin;
  1. PAIRING: a one-use code from the companion, exchanged for a per-pairing secret; a wrong code and an unsigned request are refused;
  2. DOWNLOAD (only with --provision <catalogue id> --yes): the companion's one-confirmation setup, restricted to that ONE model: disk check, download from Ollama's library, selection;
  3. SELECTION, REGISTRATION and HEALTH: the model is selected, registered for a business whose policy is LOCAL ONLY, and the companion's health is read;
  4. real questions through the two-phase pipeline (service prepare → companion → real model → service verifies every claim), plus one question the knowledge cannot answer;
  5. CANCELLATION: an answer is cancelled half way; the model's stop time is measured, the cancel is reported to the service, and the service tries NOTHING else (the answer lists one step);
  6. NO EXTERNAL INFERENCE: the operator switch has only `local` on, no provider credential exists in the process, the policy has no hosted step, and the open network connections of this process
     and of Ollama are sampled during the run: any peer that is not loopback fails the check;
  7. latency, tokens, memory (this process and Ollama, system memory and swap before and after) and failures are recorded.
It does NOT score accuracy: a handful of questions say whether the chain works and how long it takes, not how good the model is. It downloads at most the one model named by --provision and never
removes anything. Exit codes: 0 the chain ran and the report is written; 1 the chain itself failed or something external was contacted; 2 BLOCKED (no runtime, no model, no database, no room).
The report says which model, which runtime and which hardware: a result on one computer is a result on that computer.
"""
from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

MODEL_DIR = ROOT / "dist" / "bike-shop" / "model"
FAQ = ROOT / "examples" / "bike-shop" / "faq.csv"
ORIGIN = "https://app.example.com"  # the page's origin the pairing is bound to (any well-formed origin: the check plays the browser)
LOOPBACK = ("127.0.0.1", "::1", "localhost", "[::1]")


def blocked(why: str, how: str) -> None:
    print(f"BLOCKED: {why}\n  To unblock: {how}", file=sys.stderr)
    raise SystemExit(2)


class Files(dict):
    def __call__(self, path):
        return self[path]


def sh(*cmd: str, timeout: float = 10.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False).stdout  # noqa: S603 - fixed argument lists, no shell
    except (OSError, subprocess.SubprocessError):
        return ""


def system_memory() -> dict:
    """What this computer says about memory right now: free percentage, swap in use, and (macOS) free + reclaimable pages. Read-only."""
    out: dict = {}
    m = re.search(r"free percentage:\s*(\d+)%", sh("memory_pressure"))
    out["free_percentage"] = int(m.group(1)) if m else None
    s = re.search(r"used = ([\d.]+)M", sh("sysctl", "vm.swapusage"))
    out["swap_used_mb"] = float(s.group(1)) if s else None
    vm = sh("vm_stat")
    ps = re.search(r"page size of (\d+)", vm)
    pages = sum(int(x.group(1)) for x in re.finditer(r"Pages (?:free|inactive|speculative):\s+(\d+)", vm))
    out["free_reclaimable_gb"] = round(pages * int(ps.group(1)) / 2**30, 2) if ps else None
    return out


def ollama_processes() -> list[tuple[int, int, float]]:
    """(pid, resident MB, cpu%) of every process whose command mentions ollama."""
    rows = []
    for line in sh("ps", "-axo", "pid=,rss=,pcpu=,command=").splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and "ollama" in parts[3].lower() and "local_e2e_check" not in parts[3]:
            try:
                rows.append((int(parts[0]), int(parts[1]) // 1024, float(parts[2])))
            except ValueError:
                pass
    return rows


class Sampler:
    """While the check runs: peak memory of Ollama's processes and of this one, peak CPU of Ollama, and every remote address any of them has an open connection to (loopback is expected;
    anything else fails the check). Polled twice a second; a connection shorter than that could be missed, which is why the other evidence (no credentials, operator switch) is also recorded."""

    def __init__(self):
        self.peak_ollama_mb = 0
        self.peak_cpu = 0.0
        self.peak_self_mb = 0
        self.peers_by_phase: dict[str, set[str]] = {}
        self.owners: dict[str, set[str]] = {}  # peer -> which process held the connection ("ollama" or "check")
        self.phase = "inference"  # the model download legitimately talks to Ollama's library; every other phase must stay on this computer
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            procs = ollama_processes()
            self.peak_ollama_mb = max(self.peak_ollama_mb, sum(r for _, r, _ in procs))
            self.peak_cpu = max(self.peak_cpu, sum(c for _, _, c in procs))
            me = sh("ps", "-o", "rss=", "-p", str(os.getpid())).strip()
            self.peak_self_mb = max(self.peak_self_mb, int(me) // 1024 if me.isdigit() else 0)
            for pid in [os.getpid()] + [p for p, _, _ in procs]:
                for line in sh("lsof", "-nP", "-a", "-p", str(pid), "-i").splitlines()[1:]:
                    for m in re.finditer(r"->(\[?[0-9a-fA-F:.]+\]?):(\d+)(?:\s+\((\w+)\))?", line):
                        peer = m.group(1).strip("[]")
                        self.peers_by_phase.setdefault(self.phase, set()).add(peer)
                        self.owners.setdefault(peer, set()).add(f"{'check' if pid == os.getpid() else 'ollama'}:{m.group(3) or '?'}")
            self._stop.wait(0.5)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(timeout=3)

    @property
    def peers(self) -> set[str]:
        return set().union(*self.peers_by_phase.values()) if self.peers_by_phase else set()

    @staticmethod
    def _external(peers) -> list[str]:
        return sorted(p for p in peers if p not in LOOPBACK and not p.startswith("127."))

    @property
    def external(self) -> list[str]:
        """Remote peers seen at any time EXCEPT while the model was being downloaded."""
        return self._external(set().union(*[v for k, v in self.peers_by_phase.items() if k != "download"]) if self.peers_by_phase else set())

    @property
    def download_peers(self) -> list[str]:
        return self._external(self.peers_by_phase.get("download", set()))


class Client:
    """The browser's side of the companion's API: signed requests over real HTTP to the loopback service."""

    def __init__(self, port: int):
        self.port, self.secret, self.key_id = port, None, None

    def _send(self, method: str, path: str, body: dict | None, headers: dict, timeout: float) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else b""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data if method == "POST" else None, method=method, headers={"Content-Type": "application/json", "Origin": ORIGIN, **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 600.0) -> tuple[int, dict]:
        from faqengine.companion import auth

        data = json.dumps(body).encode() if body is not None else b""
        ts, nonce = str(int(time.time())), secrets.token_hex(12)
        sig = auth.sign(self.secret, method, path, ts, nonce, data)
        return self._send(method, path, body, {"X-Companion-Key": self.key_id, "X-Companion-Ts": ts, "X-Companion-Nonce": nonce, "X-Companion-Sig": sig}, timeout)

    def pair(self, code: str, label: str = "local e2e check") -> tuple[int, dict]:
        st, out = self._send("POST", "/v1/pair", {"code": code, "label": label}, {}, 30)
        if st == 200:
            self.secret, self.key_id = out["secret"], out["key_id"]
        return st, out


def registry_download_bytes(tag: str) -> int | None:
    """The download size of an Ollama library tag, from its public manifest (a read-only request), so the person can see it before anything is downloaded."""
    name, _, version = tag.partition(":")
    try:
        req = urllib.request.Request(f"https://registry.ollama.ai/v2/library/{name}/manifests/{version or 'latest'}", headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            m = json.loads(r.read())
        return sum(layer["size"] for layer in m.get("layers", [])) + m.get("config", {}).get("size", 0)
    except (OSError, ValueError, KeyError):
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="the Ollama tag to use (default: the first installed model the companion's list recommends for this computer)")
    ap.add_argument("--provision", metavar="ID", help="download this ONE model (a catalogue id such as llama3.2-1b) through the companion's setup if it is not installed; needs --yes")
    ap.add_argument("--yes", action="store_true", help="the person's confirmation for the download named by --provision")
    ap.add_argument("--ollama-base", default=None, help="Ollama's address (default http://127.0.0.1:11434; loopback only)")
    ap.add_argument("--questions", type=int, default=4)
    ap.add_argument("--device", choices=["auto", "cpu"], default="auto")
    ap.add_argument("--fast-path", action="store_true", help="leave the deterministic fast path ON (the default for the service): exact FAQ answers then never reach the model. Off by default HERE, because this check exists to exercise the model")
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds each answer may take")
    ap.add_argument("--cancel-after", type=float, default=1.5, help="seconds into a long answer before it is cancelled")
    ap.add_argument("--out", default="runs/local-e2e/report.json")
    args = ap.parse_args(argv)

    from faqengine.companion import models as cmodels
    from faqengine.companion.auth import Pairings
    from faqengine.companion.hardware import detect
    from faqengine.companion.runtimes import Ollama
    from faqengine.companion.server import Companion, make_handler

    hw = detect()
    rec = cmodels.recommend(hw)
    runtime = Ollama(args.ollama_base) if args.ollama_base else Ollama()
    det = runtime.detect()
    if not det.get("running"):
        blocked("Ollama is not running on this computer.", "install it from https://ollama.com/download (or `brew install ollama`), start it with `ollama serve`, then run this again")
    by_id = {m["id"]: m for m in rec["models"]}
    if args.provision and args.provision not in by_id:
        blocked(f"{args.provision!r} is not a model in the companion's list.", "choose an id from `faqengine companion recommend`")
    if args.provision and not args.yes:
        blocked("a download needs your confirmation.", f"run again with --provision {args.provision} --yes")
    report: dict = {"measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "machine": {"platform": platform.platform(), "python": platform.python_version(), "hardware": hw.public()},
                    "runtime": {"name": "ollama", "version": det.get("version")}, "device": args.device, "preflight": {}, "pairing": {}, "download": None, "questions": [], "notes": []}

    # ---- 0. before anything is downloaded
    free_gb = round(shutil.disk_usage(Path.home()).free / 2**30, 2)
    pre = report["preflight"] = {"memory_before": system_memory(), "free_disk_gb": free_gb, "cpu": hw.public()["cpu"], "cores": hw.public()["cores"], "ram_gb": hw.public()["ram_gb"]}
    have = runtime.list_models()
    target = by_id[args.provision]["ollama"] if args.provision else None
    if target:
        size = registry_download_bytes(target) if target not in have else 0
        pre["download_bytes"] = size
        pre["download_gb"] = None if size is None else round(size / 2**30, 2)
        pre["model_fit"] = {"verdict": by_id[args.provision]["verdict"], "needs_gb": by_id[args.provision]["needs_gb"]}
        print(f"pre-flight: {hw.public()['cpu']} · {pre['cores']} cores · {pre['ram_gb']} GB RAM ({pre['memory_before']['free_percentage']}% free, swap used {pre['memory_before']['swap_used_mb']} MB) · free disk {free_gb} GB · "
              f"{target}: fit {by_id[args.provision]['verdict']}, download {pre['download_gb']} GB", flush=True)
        if size is not None and free_gb < size / 2**30 * 1.3:
            blocked(f"the disk would not hold {target} with a margin ({free_gb} GB free, {pre['download_gb']} GB to download).", "free some space yourself; this check never removes anything")
        if by_id[args.provision]["verdict"] not in ("comfortable", "possible"):
            blocked(f"{target} is not advised on this computer ({by_id[args.provision]['why']}).", "choose a smaller model")
    model = target or args.model or next((t for t in have if any(m.get("ollama") == t and m["verdict"] in ("comfortable", "possible") for m in rec["models"])), None)
    if not model or (model not in have and not target):
        blocked(f"no suitable model is installed (installed: {', '.join(have) or 'none'}).", "run with --provision <id> --yes, or `ollama pull <tag>` yourself")
    verdict = next((m["verdict"] for m in rec["models"] if m.get("ollama") == model), "unknown")
    report["model"], report["fit_verdict"] = model, verdict

    try:
        from pgdb import make_db
    except Exception as e:  # noqa: BLE001
        blocked(f"the test database helper could not be loaded ({type(e).__name__}).", "run from the repository with its virtualenv")
    if not MODEL_DIR.is_dir():
        blocked("the exported embedding model is missing (dist/bike-shop/model).", "build it with the project's export step")
    gen = make_db()
    try:
        db = next(gen)
    except BaseException as e:  # a skip outside pytest is an exception
        blocked(f"no local PostgreSQL with pgvector ({type(e).__name__}).", "start PostgreSQL with the pgvector extension")

    from faqengine.hosted import service as svc
    from faqengine.hosted.secrets import SecretBox
    from faqengine.hosted.store import Store
    from faqengine.runtime.embedder import Embedder

    httpd = None
    try:
        with Sampler() as sampler:
            # the operator allows ONLY local inference in this process, and there is no provider credential here: nothing external could be called even by mistake
            for name in ("PLATFORM_CLOUDFLARE_API_TOKEN", "PLATFORM_OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "CLOUDFLARE_API_TOKEN"):
                os.environ.pop(name, None)
            engine = svc.HostedEngine(Store(db.dsn), Embedder(MODEL_DIR), SecretBox({1: base64.b64decode(SecretBox.generate())}), Files(), rate_per_minute=10**6, ai_modes={"local"}, inference_per_minute=10**6,
                                      pending_ttl=900.0, fast_path=args.fast_path)
            report["fast_path_enabled"] = bool(args.fast_path)
            report["external_inference_possible"] = {"operator_modes": sorted(engine.ai_modes), "platform_credentials": engine.router.platform.status()}

            # ---- 1. pairing, over real HTTP
            comp = Companion(Pairings(Path(os.environ.get("TMPDIR", "/tmp")) / f"local-e2e-{os.getpid()}.json"), runtimes={"ollama": runtime})
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(comp, 0))
            port = httpd.server_port
            httpd.RequestHandlerClass = make_handler(comp, port)
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            client = Client(port)
            wrong_code = client.pair("000-000")[0]
            unsigned = client._send("GET", "/v1/health", None, {}, 10)[0]
            st, paired = client.pair(comp.pairings.new_code())
            reuse = client._send("POST", "/v1/pair", {"code": "x", "label": "again"}, {}, 10)[0]
            report["pairing"] = {"wrong_code_refused": wrong_code in (401, 403), "unsigned_request_refused": unsigned in (401, 403), "paired": st == 200, "second_use_refused": reuse in (401, 403), "status_codes": [wrong_code, unsigned, st, reuse]}
            assert st == 200 and report["pairing"]["wrong_code_refused"] and report["pairing"]["unsigned_request_refused"], f"pairing misbehaved: {report['pairing']}"
            print(f"pairing: paired; wrong code, unsigned request and a second use were refused ({report['pairing']['status_codes']})", flush=True)

            # ---- 2. the download, only for the one model the person named
            if target and target not in have:
                t0 = time.perf_counter()
                sampler.phase = "download"
                st, prov = client.call("POST", "/v1/provision", {"confirm": True, "model": args.provision})
                assert st == 200, f"provisioning was refused: {prov}"
                job, last, ticks = prov["job"], None, []
                while True:
                    st, j = client.call("GET", f"/v1/models/pull/{job}", None, 30)
                    if j.get("total") and (not ticks or time.perf_counter() - ticks[-1][0] >= 10):
                        ticks.append((time.perf_counter(), j["done"], j["total"]))
                        print(f"  downloading {target}: {100 * j['done'] / j['total']:.0f}% of {j['total'] / 2**30:.2f} GB", flush=True)
                    if j.get("state") != "running":
                        last = j
                        break
                    time.sleep(2)
                report["download"] = {"model": target, "state": last["state"], "bytes_reported": last.get("total"), "seconds": round(time.perf_counter() - t0, 1), "selected_by_companion": last.get("selected"), "error": last.get("error"),
                                      "free_disk_gb_after": round(shutil.disk_usage(Path.home()).free / 2**30, 2)}
                sampler.phase = "inference"
                assert last["state"] == "done", f"the download failed: {last}"
                print(f"download: done in {report['download']['seconds']}s ({(last.get('total') or 0) / 2**30:.2f} GB), selected={last.get('selected')}", flush=True)
            st, sel = client.call("POST", "/v1/models/select", {"runtime": "ollama", "model": model, "device": args.device})
            assert st == 200, f"selection was refused: {sel}"
            report["selected"] = sel["selected"]

            # ---- 3. health, registration
            st, health = client.call("GET", "/v1/health")
            st2, installed = client.call("GET", "/v1/models/installed")
            report["health_before_first_answer"] = {"state": health.get("state"), "ok": health.get("ok"), "loaded": health.get("loaded"), "storage": health.get("storage")}
            report["installed"] = [{"name": m["name"], "size_gb": round((m["size_bytes"] or 0) / 2**30, 2), "selected": m["selected"]} for m in installed["runtimes"]["ollama"]["models"]]
            assert health.get("ok"), f"the companion says it is not ready: {health.get('state')}"
            print(f"health: {health['state']} · installed: {report['installed']}", flush=True)
            owner = str(db.user("local-e2e"))
            biz = str(db.business(owner, "local-e2e"))
            engine.fetch_object[f"{biz}/faq.csv"] = FAQ.read_bytes()
            sid = str(db.su("insert into public.knowledge_sources (business_id, name, source_type, storage_path) values (%s, 'faq.csv', 'faq_table', %s) returning id", (biz, f"{biz}/faq.csv"))[0][0])
            engine.ingest(biz, owner, sid)
            engine.companion_register(biz, owner, {"label": "this computer", "platform": f"{platform.system()} {platform.machine()}", "runtime": "ollama", "model": model, "fit": verdict})
            settings = engine.ai_settings_save(biz, owner, {"policy": "local_only"})
            report["policy"], report["steps"] = settings["policy"], [s["step"] for s in settings["steps"]]
            assert report["steps"] == ["local"], "the policy must have no hosted step"
            report["registered"] = [c for c in engine.companions_get(biz, owner)["companions"] if not c["revoked"]][0]["model"] == model

            # ---- 4. questions through the real pipeline
            rows = list(csv.DictReader(FAQ.open(encoding="utf-8")))
            asks = [r["question"] for r in rows[: max(1, args.questions - 1)]] + ["Do you rent submarines for deep sea diving?"]

            def ask(q: str) -> dict:
                t0 = time.perf_counter()
                prep = engine.local_prepare(biz, owner, q)
                if prep["done"]:  # answered in code (arithmetic or nothing usable): no model was asked
                    res = prep["result"]
                    return {"question": q, "model_asked": False, "outcome": res["outcome"], "generated_by": res["execution"]["generated_by"], "seconds": round(time.perf_counter() - t0, 2)}
                t1 = time.perf_counter()
                status, out = client.call("POST", "/v1/infer", {"request_id": prep["request_id"], "system": prep["prompt"]["system"], "user": prep["prompt"]["user"], "schema": prep["prompt"]["schema"], "timeout": args.timeout},
                                          args.timeout + 30)
                model_s = time.perf_counter() - t1
                fail = out.get("failure") if status == 200 else "server"
                done = engine.local_complete(biz, owner, prep["request_id"], out.get("text") or None, fail if fail in svc.HostedEngine.LOCAL_FAILURES else ("server" if fail else None), out.get("tokens_in", 0),
                                             out.get("tokens_out", 0))["result"]
                ex = done["execution"]
                return {"question": q, "model_asked": True, "outcome": done["outcome"], "generated_by": ex["generated_by"], "verified": bool(done.get("verified")), "failure_kind": ex.get("failure_kind"), "tier": ex.get("tier"),
                        "seconds": round(time.perf_counter() - t0, 2), "model_seconds": round(model_s, 2), "model_reported_seconds": out.get("seconds"), "tokens_in": out.get("tokens_in"), "tokens_out": out.get("tokens_out"),
                        "tokens_per_second": round(out["tokens_out"] / out["seconds"], 1) if out.get("tokens_out") and out.get("seconds") else None, "attempts": [a["step"] for a in ex.get("attempts", [])]}

            for q in asks:
                r = ask(q)
                report["questions"].append(r)
                print(f"  {r['seconds']:>6.1f}s  {r['generated_by']:<24} {r['outcome']:<12} {q[:58]}", flush=True)
            st, h2 = client.call("GET", "/v1/health")
            report["health_after_answers"] = {"state": h2.get("state"), "loaded": h2.get("loaded"), "stats": h2.get("stats")}

            # ---- 5. cancellation: stop a long answer, report the cancel to the service, and see that NOTHING else is tried
            prep = engine.local_prepare(biz, owner, asks[0])
            cancel: dict = {"service_prepared": not prep["done"]}
            if not prep["done"]:
                rid = prep["request_id"]
                holder: dict = {}
                long_user = prep["prompt"]["user"] + "\n\nWrite a very long, detailed answer of at least 2000 words before the JSON."
                t = threading.Thread(target=lambda: holder.update(zip(("status", "out"), client.call("POST", "/v1/infer", {"request_id": rid, "system": "You are thorough.", "user": long_user, "timeout": args.timeout}, args.timeout + 30))))
                t.start()
                time.sleep(args.cancel_after)
                asked = time.perf_counter()
                cst, cres = client.call("POST", "/v1/cancel", {"request_id": rid})
                t.join(90)
                stopped_in = round(time.perf_counter() - asked, 2)
                out = holder.get("out") or {}
                cancel.update({"stopped": not t.is_alive(), "was_still_answering": bool(cres.get("cancelled")), "seconds_to_stop": stopped_in, "companion_failure": out.get("failure")})
                done = engine.local_complete(biz, owner, rid, None, "cancelled", out.get("tokens_in", 0), out.get("tokens_out", 0))["result"]["execution"]
                cancel.update({"service_failure_kind": done.get("failure_kind"), "service_generated_by": done.get("generated_by"), "steps_tried": [(a["step"], a["outcome"]) for a in done.get("attempts", [])],
                               "nothing_else_tried": [a["step"] for a in done.get("attempts", [])] == ["local"] and done.get("tier") == "local"})
                if not cancel["was_still_answering"]:
                    report["notes"].append("the answer had already finished when it was cancelled, so the cancellation was not exercised: run again with a smaller --cancel-after")
                print(f"  cancel: stopped={cancel['stopped']} in {stopped_in}s (still answering: {cancel['was_still_answering']}); service: {cancel.get('service_failure_kind')}, steps {cancel['steps_tried']}", flush=True)
            report["cancellation"] = cancel

            used = db.su("select tier, mode, count(*) from public.usage_records where business_id = %s and kind = 'llm_call' group by 1, 2", (biz,))
            report["metering"] = [{"tier": a, "mode": b, "calls": c} for a, b, c in used]
            q = report["questions"]
            asked_model = [x for x in q if x["model_asked"]]
            report["summary"] = {"asked": len(q), "model_asked": len(asked_model), "generated_by_model": sum(x["generated_by"] == "model" for x in q), "verified": sum(bool(x.get("verified")) for x in q),
                                 "failures": sum(bool(x.get("failure_kind")) for x in q), "median_seconds": sorted(x["seconds"] for x in q)[len(q) // 2] if q else None, "max_seconds": max((x["seconds"] for x in q), default=None),
                                 "median_model_seconds": sorted(x["model_seconds"] for x in asked_model)[len(asked_model) // 2] if asked_model else None,
                                 "median_tokens_per_second": sorted(x["tokens_per_second"] for x in asked_model if x.get("tokens_per_second"))[len([x for x in asked_model if x.get("tokens_per_second")]) // 2] if any(x.get("tokens_per_second") for x in asked_model) else None}
        # ---- 6 and 7. what the sampler saw
        report["memory"] = {"before": pre["memory_before"], "after": system_memory(), "peak_ollama_mb": sampler.peak_ollama_mb, "peak_this_process_mb": sampler.peak_self_mb, "peak_ollama_cpu_percent": round(sampler.peak_cpu, 1)}
        report["network"] = {"peers_seen": sorted(sampler.peers), "external_peers_outside_the_download": sampler.external, "peers_during_the_download_only": sampler.download_peers,
                             "who_held_each_external_peer": {p: sorted(sampler.owners.get(p, [])) for p in sorted(set(sampler.external) | set(sampler.download_peers))}}
        report["nothing_external_was_called"] = (not sampler.external and all(r.get("tier") in (None, "local") and r.get("attempts", ["local"]) == ["local"] for r in report["questions"])
                                                  and all(a == "local" for a, _, _ in used) and report["steps"] == ["local"] and cancel.get("nothing_else_tried", True) is True)
        report["notes"] += ["a few questions say the chain works and how long it takes; they do not measure how good the model is", "the machine's other load, heat and memory pressure change the times",
                            "the network sample polls twice a second and could miss a connection shorter than that: the operator switch and the absence of credentials are the stronger evidence"]
        if not report["nothing_external_was_called"]:
            print(f"SOMETHING EXTERNAL MAY HAVE BEEN CONTACTED: {report['network']}", file=sys.stderr)
    except AssertionError as e:
        print(f"the chain failed: {e}", file=sys.stderr)
        return 1
    finally:
        if httpd is not None:
            httpd.shutdown()
        next(gen, None)
    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=1))
    print(f"wrote {out_path}")
    return 0 if report["nothing_external_was_called"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
