"""THE COMPANION'S LOCAL HTTP SERVICE. Bound to 127.0.0.1 only (there is no option to bind elsewhere). What answers without a signature: `GET /v1/hello` (a companion is here: nothing else) and the pairing
handshake. Everything else needs a valid signature (auth.py), a Host that is this computer's loopback address and, from a browser, an Origin the person paired from. One inference runs at a time
(a second is told to wait), with a deadline, and can be cancelled by its request id; cancelling closes the connection to the runtime, which stops it generating. Prompts and answers are never logged:
only the kind of event, the model's name and sizes."""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import hardware, models
from .auth import ORIGIN, Pairings, host_ok
from .lifecycle import OllamaSupervisor
from .runtimes import LlamaCpp, Ollama, Runtime, RuntimeError_

log = logging.getLogger("faqengine.companion")
VERSION = "0.1.0"
MAX_BODY = 1_000_000
MAX_PROMPT = 200_000
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")
CORS_HEADERS = "Content-Type, X-Companion-Key, X-Companion-Ts, X-Companion-Nonce, X-Companion-Sig, X-Companion-Business"


class Companion:
    def __init__(self, pairings: Pairings, runtimes: dict[str, Runtime] | None = None, detect=hardware.detect, allow_custom_models: bool = False, supervisor: OllamaSupervisor | None = None):
        self.pairings = pairings
        self.runtimes = runtimes if runtimes is not None else {"ollama": Ollama(), "llamacpp": LlamaCpp()}
        ol = self.runtimes.get("ollama")
        self.supervisor = supervisor if supervisor is not None else (OllamaSupervisor(ol) if isinstance(ol, Ollama) and ol.base == Ollama.default_base else None)  # only the real runtime on its own address is ever started: a stand-in is left alone
        self.detect, self.allow_custom_models = detect, allow_custom_models
        self._busy = threading.Semaphore(1)
        self._active: dict[str, threading.Event] = {}
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._hw: tuple[float, hardware.Hardware] | None = None
        self.stats = {"questions": 0, "failures": 0, "last_seconds": None, "last_failure": None, "last_at": None}  # counts and times only: never what was asked or answered

    # ---- what it can say
    def hardware(self) -> hardware.Hardware:
        if self._hw is None or time.time() - self._hw[0] > 60:
            self._hw = (time.time(), self.detect())
        return self._hw[1]

    def status(self) -> dict:
        rts = {}
        for name, rt in self.runtimes.items():
            d = rt.detect()
            try:
                d["models"] = rt.list_models() if d["running"] or name == "llamacpp" else []
            except RuntimeError_:
                d["models"] = []
            rts[name] = d
        return {"companion": True, "version": VERSION, "hardware": self.hardware().public(), "runtimes": rts, "selected": self.pairings.selected or None, "busy": self._busy._value == 0,
                "runtime_state": self.supervisor.state() if self.supervisor else None}

    # ---- models
    def select(self, runtime: object, model: object, device: object = "auto") -> dict:
        rt = self.runtimes.get(runtime) if isinstance(runtime, str) else None
        if rt is None or not isinstance(model, str) or not MODEL_NAME.match(model):
            raise ValueError("choose a runtime and a model")
        if device not in ("auto", "cpu"):
            raise ValueError("the device is auto or cpu")
        if model not in rt.list_models():
            raise ValueError("that model is not installed in that runtime")
        self.pairings.select(runtime, model, device)
        return {"selected": self.pairings.selected}

    def _download_gb(self, model: str) -> float | None:
        """A rough size for a catalogue model's download (the weights at the quantisation the estimate assumes), for the disk check. None for a model that is not in the list."""
        m = next((x for x in models.catalogue() if x.ollama == model), None)
        return None if m is None else round(m.params_b * models.BYTES_PER_PARAM[m.quant] * 1.05, 1)

    def pull(self, runtime: object, model: object, then_select: bool = False, update: bool = False) -> dict:
        rt = self.runtimes.get(runtime) if isinstance(runtime, str) else None
        if not isinstance(rt, Ollama) or not isinstance(model, str) or not MODEL_NAME.match(model):
            raise ValueError("downloads are made through Ollama, for a named model")
        known = {m.ollama for m in models.catalogue() if m.ollama}
        if model not in known and not self.allow_custom_models:
            raise ValueError("that model is not in the companion's list; add it to the list or start the companion with custom models allowed")
        with self._lock:  # one download per model: a second request while one runs gets the same job (and then_select is remembered), never a second download
            running = next((j for j, v in self._jobs.items() if v["model"] == model and v["state"] == "running"), None)
            if running:
                self._jobs[running]["select_when_done"] = self._jobs[running].get("select_when_done") or then_select
                return {"job": running}
            job = uuid.uuid4().hex[:16]
            self._jobs[job] = {"state": "running", "done": 0, "total": 0, "error": None, "model": model, "selected": False, "updated": None, "select_when_done": then_select}
            cancel = self._cancels[job] = threading.Event()
        before = next((m["digest"] for m in rt.installed() if m["name"] == model), None) if update else None

        def work():
            try:
                rt.pull(model, lambda d, t: self._jobs[job].update(done=d, total=t), cancel)
                if update:
                    after = next((m["digest"] for m in rt.installed() if m["name"] == model), None)
                    self._jobs[job]["updated"] = before is not None and after is not None and before != after
                if cancel.is_set():
                    self._jobs[job]["state"] = "cancelled"
                    return
                if self._jobs[job].get("select_when_done"):
                    self.pairings.select("ollama", model)
                    self._jobs[job]["selected"] = True
                self._jobs[job]["state"] = "done"
            except RuntimeError_ as e:
                self._jobs[job].update(state="cancelled" if cancel.is_set() else "failed", error=None if cancel.is_set() else e.kind)

        threading.Thread(target=work, daemon=True).start()
        return {"job": job}

    def provision(self, confirm: object, model_id: object = None) -> dict:
        """SET UP LOCAL AI WITH ONE CONFIRMATION: when nothing is installed, pick the model this computer can run best (or the one the person chose from the list), check the runtime is there and the disk
        has room, download it, and select it for answering. Nothing is downloaded unless `confirm` is exactly true: the person is shown the model, its approximate size and the free space first. The
        runtime itself (Ollama) is a separate program the person installs; this does not install software."""
        if confirm is not True:
            raise ValueError("downloading a model needs the person's confirmation")
        rt = self.runtimes.get("ollama")
        d = rt.detect() if rt else {"installed": False, "running": False}
        if not isinstance(rt, Ollama):
            raise ValueError("the model runtime is not available on this computer")
        if not d.get("running") and self.supervisor is not None:
            self.supervisor.ensure_running()  # installed but not running: start it (never a second copy)
            d = rt.detect()
        if not d.get("running"):
            why = (self.supervisor.last_error if self.supervisor else None) or "could_not_start"
            raise ValueError({"not_installed": "The AI runtime (Ollama) is not installed on this computer yet. The Knovent installer can set it up for you.", "gave_up": "The AI runtime keeps stopping. Restart your computer, then try again."}.get(why, "The AI runtime could not be started. Try again in a moment."))
        rec = models.recommend(self.hardware())
        pick = model_id if isinstance(model_id, str) and model_id else rec["start_with"]
        row = next((r for r in rec["models"] if r["id"] == pick), None)
        if row is None or not row.get("ollama"):
            raise ValueError("no model in the list fits this computer" if pick is None else "that model is not in the list")
        if row["verdict"] not in ("comfortable", "possible"):
            raise ValueError(f"{row['id']} is not advised on this computer: {row['why']}")
        tag = row["ollama"]
        have = rt.list_models()
        if tag in have:
            self.pairings.select("ollama", tag)
            return {"job": None, "model": tag, "already_installed": True, "selected": True, "download_gb_estimate": 0.0, "free_gb": self.storage()["free_gb"], "fit": row["verdict"]}
        need = self._download_gb(tag) or round(row["needs_gb"], 1)
        free = self.storage()["free_gb"]
        if free is not None and free < need * 1.3:
            raise ValueError(f"not enough free disk space: about {need} GB is needed for {row['id']} and {free} GB is free")
        job = self.pull("ollama", tag, then_select=True)["job"]
        return {"job": job, "model": tag, "already_installed": False, "selected": False, "download_gb_estimate": need, "free_gb": free, "fit": row["verdict"]}

    def delete(self, runtime: object, model: object) -> dict:
        rt = self.runtimes.get(runtime) if isinstance(runtime, str) else None
        if not isinstance(rt, Ollama) or not isinstance(model, str) or not MODEL_NAME.match(model):
            raise ValueError("models are removed through Ollama, by name")
        sel = self.pairings.selected
        if sel.get("runtime") == runtime and sel.get("model") == model:
            raise ValueError("that is the model in use: choose another first")
        if model not in rt.list_models():
            raise ValueError("that model is not installed")
        rt.delete(model)
        return {"removed": model}

    def update(self, runtime: object, model: object) -> dict:
        """Asks Ollama for the newest version of an installed model (its own library, its own terms); the job says whether anything changed."""
        if not isinstance(runtime, str) or not isinstance(model, str) or runtime != "ollama" or model not in self.runtimes["ollama"].list_models():
            raise ValueError("that model is not installed in Ollama")
        return self.pull(runtime, model, update=True)

    # ---- what is installed, what it takes, whether it is healthy
    def installed(self) -> dict:
        out = {}
        sel = self.pairings.selected
        for name, rt in self.runtimes.items():
            d = rt.detect()
            if not d.get("running") and name != "llamacpp":
                out[name] = {"running": False, "models": []}
                continue
            try:
                out[name] = {"running": bool(d.get("running")), "models": [{**m, "selected": sel.get("runtime") == name and sel.get("model") == m["name"], "in_catalogue": any(x.ollama == m["name"] for x in models.catalogue())}
                                                                           for m in rt.installed()]}
            except RuntimeError_:
                out[name] = {"running": False, "models": []}
        return {"runtimes": out, "selected": sel or None}

    def storage(self) -> dict:
        """Free space on the volume the models live on (the home folder's, where Ollama and the companion keep them) and what the installed models take."""
        try:
            free = round(shutil.disk_usage(Path.home()).free / 2**30, 1)
        except OSError:
            free = None
        used = 0
        for rt in self.runtimes.values():
            try:
                used += sum(m["size_bytes"] or 0 for m in rt.installed())
            except RuntimeError_:
                pass
        return {"free_gb": free, "models_gb": round(used / 2**30, 2)}

    def health(self) -> dict:
        """Is local answering ready right now, and if not, why: the runtime running, the chosen model installed, whether it is loaded in memory, whether another question is being answered, and how the
        last questions went (counts and times only)."""
        sel = self.pairings.selected
        rt = self.runtimes.get(sel.get("runtime", "")) if sel else None
        base = {"selected": sel or None, "busy": self._busy._value == 0, "stats": dict(self.stats), "storage": self.storage()}
        if rt is None or not sel.get("model"):
            return {**base, "state": "no_model_selected", "ok": False}
        d = rt.detect()
        if not d.get("running") and sel["runtime"] != "llamacpp":
            return {**base, "state": "runtime_not_running", "ok": False, "runtime": d}
        try:
            present = sel["model"] in rt.list_models()
        except RuntimeError_:
            return {**base, "state": "runtime_not_running", "ok": False, "runtime": d}
        if not present:
            return {**base, "state": "model_missing", "ok": False, "runtime": d}
        return {**base, "state": "busy" if base["busy"] else "ready", "ok": True, "runtime": d, "loaded": sel["model"] in rt.loaded()}

    def cancel_download(self, job_id: object) -> bool:
        """Stops a running download. What was already fetched stays, and a later download of the same model continues from it."""
        ev = self._cancels.get(job_id) if isinstance(job_id, str) else None
        if ev is None or self._jobs.get(job_id, {}).get("state") != "running":
            return False
        ev.set()
        return True

    def job(self, job_id: object) -> dict | None:
        return self._jobs.get(job_id) if isinstance(job_id, str) else None

    # ---- inference
    def infer(self, body: dict) -> tuple[int, dict]:
        rid, system, user, schema = body.get("request_id"), body.get("system"), body.get("user"), body.get("schema")
        if not isinstance(rid, str) or not re.fullmatch(r"[0-9A-Za-z_-]{8,64}", rid) or not isinstance(system, str) or not isinstance(user, str) or len(system) + len(user) > MAX_PROMPT or not (schema is None or isinstance(schema, dict)):
            return 400, {"error": "the request is not valid"}
        sel = self.pairings.selected
        rt = self.runtimes.get(sel.get("runtime", ""))
        if rt is None or not sel.get("model"):
            return 409, {"error": "no model is selected on this computer"}
        timeout = body.get("timeout")
        timeout = float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and 1 <= timeout <= 900 else 300.0
        if not self._busy.acquire(blocking=False):
            return 429, {"error": "another question is being answered; try again in a moment"}
        stop = threading.Event()
        with self._lock:
            self._active[rid] = stop
        try:
            r = rt.complete(sel["model"], system, user, schema, stop, timeout, int(body["max_tokens"]) if isinstance(body.get("max_tokens"), int) and 1 <= body["max_tokens"] <= 4000 else 1500,
                            device=sel.get("device", "auto"))
        finally:
            with self._lock:
                self._active.pop(rid, None)
            self._busy.release()
        with self._lock:
            self.stats.update(questions=self.stats["questions"] + 1, failures=self.stats["failures"] + bool(r.failure), last_seconds=round(r.seconds, 2), last_failure=(r.failure or "").removeprefix("provider:") or None,
                              last_at=int(time.time()))
        log.info("inference finished: runtime=%s model=%s failure=%s tokens_in=%s tokens_out=%s seconds=%.1f", sel["runtime"], sel["model"], r.failure, r.tokens_in, r.tokens_out, r.seconds)  # never the text
        return 200, {"text": r.text, "failure": (r.failure or "").removeprefix("provider:") or None, "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "seconds": round(r.seconds, 2), "runtime": sel["runtime"], "model": sel["model"]}

    def cancel(self, rid: object) -> bool:
        with self._lock:
            ev = self._active.get(rid) if isinstance(rid, str) else None
        if ev:
            ev.set()
        return ev is not None


# the application served on this very computer (the preview or a local install): its origin is loopback, which no remote website can claim
LOCAL_APP = re.compile(r"^http://(127\.0\.0\.1|localhost)(:\d{1,5})?$")


def make_handler(c: Companion, port: int, extra_origins: set[str] | None = None):
    signed_get = {"/v1/status": lambda q: (200, c.status()), "/v1/recommend": lambda q: (200, models.recommend(c.hardware())), "/v1/pairings": lambda q: (200, {"pairings": c.pairings.listing()}),
                  "/v1/models/installed": lambda q: (200, c.installed()), "/v1/health": lambda q: (200, c.health()), "/v1/storage": lambda q: (200, c.storage())}

    class Handler(BaseHTTPRequestHandler):
        server_version = "faqengine-companion"
        sys_version = ""

        # ---- plumbing
        def _origin(self) -> str | None:
            o = self.headers.get("Origin")
            return o if o and ORIGIN.match(o) else None

        def _cors(self, origin: str | None) -> dict:
            if not origin:
                return {}
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin", "Access-Control-Allow-Headers": CORS_HEADERS, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Max-Age": "600"}

        def _send(self, status: int, body: dict, origin: str | None = None, extra: dict | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in {**self._cors(origin), **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> bytes | None:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= MAX_BODY:
                return None
            return self.rfile.read(n) if n else b""

        def _refuse(self, why: str, body: dict | None = None) -> None:
            """A refusal. The browser is allowed to READ it (CORS headers, only for a well formed Origin): otherwise it hides the 401 and the page reports "not reachable" about a companion that answered.
            Reading a refusal grants nothing. Signed requests always get the same words (no hint of which check failed); only the unsigned pairing step says why, because the person needs to know."""
            log.warning("request refused: %s (%s)", why, self.path.split("?")[0])
            origin = self._origin() if host_ok(self.headers.get("Host"), port) else None
            self._send(401, body or {"error": "not authorised"}, origin)

        def log_message(self, fmt, *args):  # no access log with addresses of prompts: nothing is logged here
            pass

        # ---- methods
        def do_OPTIONS(self):
            origin = self._origin()
            if not host_ok(self.headers.get("Host"), port) or origin is None:
                return self._send(403, {"error": "not allowed"})
            if self.path in ("/v1/hello", "/v1/pair") or origin in c.pairings.allowed_origins() or origin in (extra_origins or set()):
                extra = {"Access-Control-Allow-Private-Network": "true"} if self.headers.get("Access-Control-Request-Private-Network") == "true" else {}
                return self._send(204, {}, origin, extra)
            self._send(403, {"error": "not allowed"})

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str):
            if not host_ok(self.headers.get("Host"), port):
                return self._refuse("host header")
            origin = self._origin()
            if self.headers.get("Origin") and origin is None:
                return self._refuse("origin malformed")
            path = self.path.split("?")[0]
            body = self._body()
            if body is None:
                return self._send(413, {"error": "the request is too large"})
            if method == "GET" and path == "/v1/hello":
                return self._send(200, {"companion": True, "version": VERSION, "pairing_open": c.pairings._code is not None}, origin)
            if method == "POST" and path == "/v1/pair":
                try:
                    d = json.loads(body or b"{}")
                except ValueError:
                    return self._send(400, {"error": "not JSON"}, origin)
                d = d if isinstance(d, dict) else {}
                trusted = origin is not None and (origin in (extra_origins or set()) or LOCAL_APP.match(origin))
                biz = d.get("business") if isinstance(d.get("business"), str) else None
                got = c.pairings.pair_trusted(origin, d.get("label"), biz) if trusted and not d.get("code") else c.pairings.pair(d.get("code"), origin, d.get("label"), biz)
                if got is None:
                    if d.get("code"):
                        return self._refuse("pairing code", {"error": "code_not_accepted"})
                    return self._refuse("origin not trusted", {"error": "origin_not_trusted"})  # no code and not a trusted address: the page must say so, and offer the installer or the code
                return self._send(200, got, origin)
            key_id, why = c.pairings.verify(method, path, body, {k: self.headers.get(k, "") for k in ("X-Companion-Key", "X-Companion-Ts", "X-Companion-Nonce", "X-Companion-Sig", "X-Companion-Business")})
            if key_id is None:
                return self._refuse(why or "signature")
            if origin is not None and origin != c.pairings.origin_of(key_id):
                return self._refuse("origin is not the paired one")  # a signature made for another origin is not accepted from this one
            try:
                d = json.loads(body) if body else {}
                if not isinstance(d, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "not a JSON object"}, origin)
            try:
                if method == "GET" and path in signed_get:
                    status, out = signed_get[path](d)
                elif method == "GET" and path.startswith("/v1/models/pull/"):
                    j = c.job(path.rsplit("/", 1)[1])
                    status, out = (200, j) if j else (404, {"error": "no such download"})
                elif method == "POST" and path == "/v1/models/select":
                    status, out = 200, c.select(d.get("runtime"), d.get("model"), d.get("device", "auto"))
                elif method == "POST" and path == "/v1/models/delete":
                    status, out = 200, c.delete(d.get("runtime"), d.get("model"))
                elif method == "POST" and path == "/v1/models/update":
                    status, out = 200, c.update(d.get("runtime"), d.get("model"))
                elif method == "POST" and path == "/v1/provision":
                    status, out = 200, c.provision(d.get("confirm"), d.get("model"))
                elif method == "POST" and path == "/v1/models/pull/cancel":
                    status, out = 200, {"cancelled": c.cancel_download(d.get("job"))}
                elif method == "POST" and path == "/v1/models/pull":
                    status, out = 200, c.pull(d.get("runtime"), d.get("model"))
                elif method == "POST" and path == "/v1/infer":
                    status, out = c.infer(d)
                elif method == "POST" and path == "/v1/cancel":
                    status, out = 200, {"cancelled": c.cancel(d.get("request_id"))}
                elif method == "POST" and path == "/v1/unpair":
                    kid = d.get("key_id") or key_id
                    status, out = 200, {"removed": c.pairings.remove(kid) if isinstance(kid, str) else False}
                else:
                    status, out = 404, {"error": "not found"}
            except ValueError as e:
                status, out = 400, {"error": str(e)}
            except RuntimeError_ as e:
                status, out = 502, {"error": e.kind}
            self._send(status, out, origin)

    return Handler


def serve(port: int = 47890, companion: Companion | None = None, extra_origins: set[str] | None = None, announce=print) -> ThreadingHTTPServer:
    """Starts the service on 127.0.0.1 (there is no way to choose another address) and returns it; the caller runs `serve_forever()`."""
    comp = companion or Companion(Pairings())
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(comp, port, extra_origins))
    httpd.companion = comp  # type: ignore[attr-defined]
    announce(f"faqengine companion {VERSION} listening on 127.0.0.1:{httpd.server_port} (this computer only)")
    return httpd
