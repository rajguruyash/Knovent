"""LOCAL RUNTIMES: Ollama and llama.cpp. Each is reached on loopback only (a configured address that is not 127.0.0.1, localhost or ::1 is refused), speaks over streaming HTTP, and can be CANCELLED
for real: the response is read line by line, and when cancellation is set or the deadline passes the connection is closed, which makes the runtime stop generating. Nothing here knows a business or
a document; it is handed a prompt and returns text. A downloaded model file is size-limited, hash-checked when a hash is given, and fetched only from an allow-listed host over https."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path

from ..runtime.contracts import ProviderReply

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_REPLY_BYTES = 2_000_000
DEFAULT_DOWNLOAD_HOSTS = ("huggingface.co", "cdn-lfs.huggingface.co")
MAX_DOWNLOAD_BYTES = 40 * 2**30


class RuntimeError_(Exception):
    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def loopback_connection(base: str, timeout: float) -> tuple[http.client.HTTPConnection, str]:
    u = urllib.parse.urlparse(base)
    if u.scheme != "http" or (u.hostname or "") not in LOOPBACK:
        raise RuntimeError_("blocked", "the runtime must be on this computer (http://127.0.0.1)")
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout), u.path.rstrip("/")


class Runtime:
    name = "base"
    default_base = ""

    def __init__(self, base: str | None = None, timeout: float = 600.0):
        self.base, self.timeout = (base or self.default_base).rstrip("/"), timeout
        loopback_connection(self.base, 1)  # refuses a base that is not loopback, now

    def _get(self, path: str, timeout: float = 5.0) -> dict:
        conn, prefix = loopback_connection(self.base, timeout)
        try:
            conn.request("GET", prefix + path)
            r = conn.getresponse()
            raw = r.read(MAX_REPLY_BYTES)
            if r.status != 200:
                raise RuntimeError_("server", f"HTTP {r.status}")
            return json.loads(raw or b"{}")
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise RuntimeError_("not_connected", type(e).__name__) from None
        finally:
            conn.close()

    def _stream(self, path: str, body: dict, cancel: threading.Event | None, deadline: float):
        """Yields the response's lines. A watcher wakes on cancellation or at the deadline and shuts the socket down, which interrupts a read that is blocked on a silent runtime (a model that is
        still loading) and makes the runtime stop generating; the reader then reports why: `cancelled` or `timeout`."""
        conn, prefix = loopback_connection(self.base, min(self.timeout, 60.0))
        why: list[str] = []
        done = threading.Event()
        try:
            conn.connect()
            sock = conn.sock

            def watch() -> None:
                while not done.is_set():
                    if cancel is not None and cancel.is_set():
                        why.append("cancelled")
                    elif time.monotonic() > deadline:
                        why.append("timeout")
                    if why:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        return
                    done.wait(0.05)

            threading.Thread(target=watch, daemon=True).start()
            sock.settimeout(None)  # the watcher, not a socket timeout, ends a wait
            conn.request("POST", prefix + path, body=json.dumps(body), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                data = resp.read(4096).decode("utf-8", "replace")
                raise RuntimeError_("not_found" if resp.status == 404 else "bad_request" if resp.status == 400 else "server", f"HTTP {resp.status}: {data[:120]}")
            total = 0
            while True:
                line = resp.readline()
                if why:
                    raise RuntimeError_(why[0])
                if not line:
                    return
                total += len(line)
                if total > MAX_REPLY_BYTES:
                    raise RuntimeError_("server", "the runtime's answer was too large")
                if line.strip():
                    yield line
        except (OSError, http.client.HTTPException) as e:
            if why:
                raise RuntimeError_(why[0]) from None
            if isinstance(e, http.client.HTTPException):
                raise RuntimeError_("server", type(e).__name__) from None
            raise RuntimeError_("not_connected", type(e).__name__) from None
        finally:
            done.set()
            conn.close()  # on cancellation this is what stops the runtime

    def detect(self) -> dict:  # pragma: no cover - interface
        raise NotImplementedError

    def installed(self) -> list[dict]:
        """The models this runtime holds, with what is known about each (`size_bytes` and `digest` are None where the runtime does not say)."""
        return [{"name": n, "size_bytes": None, "digest": None, "modified": None} for n in self.list_models()]

    def loaded(self) -> list[str]:
        """The models currently held in memory, where the runtime says (empty otherwise)."""
        return []

    def list_models(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def complete(self, model: str, system: str, user: str, schema: dict | None, cancel: threading.Event | None = None, timeout: float | None = None, max_tokens: int = 1500,
                 device: str = "auto") -> ProviderReply:  # pragma: no cover
        raise NotImplementedError


class Ollama(Runtime):
    name = "ollama"
    default_base = "http://127.0.0.1:11434"

    def detect(self) -> dict:
        try:
            v = self._get("/api/version", 2.0)
            return {"runtime": "ollama", "installed": True, "running": True, "version": str(v.get("version", ""))[:40]}
        except RuntimeError_:
            return {"runtime": "ollama", "installed": shutil.which("ollama") is not None, "running": False, "version": None}

    def list_models(self) -> list[str]:
        return sorted(m.get("name", "") for m in self._get("/api/tags").get("models", []) if m.get("name"))

    def installed(self) -> list[dict]:
        return sorted(({"name": m.get("name", ""), "size_bytes": int(m.get("size") or 0) or None, "digest": str(m.get("digest") or "")[:19] or None, "modified": str(m.get("modified_at") or "")[:25] or None}
                       for m in self._get("/api/tags").get("models", []) if m.get("name")), key=lambda d: d["name"])

    def loaded(self) -> list[str]:
        try:
            return sorted(m.get("name", "") for m in self._get("/api/ps").get("models", []) if m.get("name"))
        except RuntimeError_:
            return []

    def pull(self, model: str, progress=None, cancel: threading.Event | None = None) -> None:
        """Asks Ollama to download a model (Ollama's own library and its own terms); `progress(done, total)` is called as it goes."""
        layers: dict[str, tuple[int, int]] = {}  # Ollama reports each layer (the weights, a template, parameters...) on its own: the progress is the SUM over layers, not the last line's
        for line in self._stream("/api/pull", {"model": model, "stream": True}, cancel, time.monotonic() + 6 * 3600):
            d = json.loads(line)
            if d.get("error"):
                raise RuntimeError_("not_found", str(d["error"])[:120])
            if progress and d.get("total"):
                layers[str(d.get("digest") or "")] = (int(d.get("completed") or 0), int(d["total"]))
                progress(sum(c for c, _ in layers.values()), sum(t for _, t in layers.values()))

    def delete(self, model: str) -> None:
        conn, prefix = loopback_connection(self.base, 30)
        try:
            conn.request("DELETE", prefix + "/api/delete", body=json.dumps({"model": model}), headers={"Content-Type": "application/json"})
            if conn.getresponse().status not in (200, 404):
                raise RuntimeError_("server")
        except OSError:
            raise RuntimeError_("not_connected") from None
        finally:
            conn.close()

    def complete(self, model, system, user, schema, cancel=None, timeout=None, max_tokens=1500, device: str = "auto") -> ProviderReply:
        started = time.monotonic()
        body = {"model": model, "stream": True, "think": False, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "options": {"temperature": 0, "num_predict": max_tokens}}
        if device == "cpu":
            body["options"]["num_gpu"] = 0  # no layers on the GPU: slower, and it works where the GPU's memory does not suffice
        if schema:
            body["format"] = schema
        text, tin, tout = [], 0, 0
        try:
            for line in self._stream("/api/chat", body, cancel, started + (timeout or self.timeout)):
                d = json.loads(line)
                if d.get("error"):
                    raise RuntimeError_("not_found" if "not found" in str(d["error"]).lower() else "server", str(d["error"])[:120])
                text.append((d.get("message") or {}).get("content", ""))
                if d.get("done"):
                    tin, tout = int(d.get("prompt_eval_count") or 0), int(d.get("eval_count") or 0)
        except RuntimeError_ as e:
            return ProviderReply(failure=f"provider:{e.kind}", seconds=time.monotonic() - started, model=model)
        except ValueError:
            return ProviderReply(failure="provider:server", seconds=time.monotonic() - started, model=model)
        return ProviderReply(text="".join(text), tokens_in=tin, tokens_out=tout, seconds=time.monotonic() - started, model=model)


class LlamaCpp(Runtime):
    """llama.cpp's `llama-server` (OpenAI-compatible, one model loaded per process). The companion can start and stop it for a model file it manages."""

    name = "llamacpp"
    default_base = "http://127.0.0.1:8080"

    def __init__(self, base: str | None = None, timeout: float = 600.0, models_dir: str | None = None, binary: str | None = None):
        super().__init__(base, timeout)
        self.models_dir = Path(models_dir or Path.home() / ".local" / "share" / "faq-engine" / "models")
        self.binary = binary or shutil.which("llama-server")
        self._proc: subprocess.Popen | None = None

    def detect(self) -> dict:
        installed = self.binary is not None
        try:
            self._get("/health", 2.0)
            return {"runtime": "llamacpp", "installed": installed, "running": True, "version": None}
        except RuntimeError_:
            return {"runtime": "llamacpp", "installed": installed, "running": False, "version": None}

    def list_models(self) -> list[str]:
        """The model files the companion manages (what it could start), and, if a server is running, the one it has loaded."""
        files = sorted(p.name for p in self.models_dir.glob("*.gguf")) if self.models_dir.is_dir() else []
        try:
            loaded = [m.get("id", "") for m in self._get("/v1/models").get("data", []) if m.get("id")]
        except RuntimeError_:
            loaded = []
        return sorted(set(files) | set(loaded))

    def installed(self) -> list[dict]:
        files = sorted(self.models_dir.glob("*.gguf")) if self.models_dir.is_dir() else []
        return [{"name": f.name, "size_bytes": f.stat().st_size, "digest": None, "modified": None} for f in files]

    def start(self, model_file: str, ctx: int = 8192, threads: int | None = None, gpu_layers: int = 0) -> None:
        """Starts llama-server on the loopback port for one of the managed model files (a file name, never a path)."""
        if self.binary is None:
            raise RuntimeError_("not_found", "llama-server is not installed")
        if os.path.basename(model_file) != model_file or not model_file.endswith(".gguf") or not (self.models_dir / model_file).is_file():
            raise RuntimeError_("not_found", "that model file is not one the companion manages")
        self.stop()
        port = urllib.parse.urlparse(self.base).port or 8080
        cmd = [self.binary, "-m", str(self.models_dir / model_file), "--host", "127.0.0.1", "--port", str(port), "-c", str(ctx), "-ngl", str(gpu_layers)] + (["-t", str(threads)] if threads else [])
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603 - a fixed argument list, no shell, a file name checked above

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    def download(self, url: str, name: str, sha256: str | None = None, allowed_hosts: tuple[str, ...] = DEFAULT_DOWNLOAD_HOSTS, max_bytes: int = MAX_DOWNLOAD_BYTES, progress=None, cancel=None) -> Path:
        """Fetches a GGUF model file the person asked for: https only, from an allow-listed host, to a file name the companion chooses, size-limited, hash-checked when a hash is given, and moved into
        place only when complete."""
        import ssl
        import urllib.request

        u = urllib.parse.urlparse(url)
        if u.scheme != "https" or (u.hostname or "") not in allowed_hosts:
            raise RuntimeError_("blocked", "models are downloaded over https from an allow-listed host only")
        if os.path.basename(name) != name or not name.endswith(".gguf"):
            raise RuntimeError_("bad_request", "the file name must be a plain name ending in .gguf")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        part, final = self.models_dir / (name + ".part"), self.models_dir / name
        h = hashlib.sha256()
        done = 0
        req = urllib.request.Request(url, headers={"User-Agent": "faq-engine-companion"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r, open(part, "wb") as f:  # noqa: S310 - https and the host were checked above
                total = int(r.headers.get("Content-Length") or 0)
                if total > max_bytes:
                    raise RuntimeError_("blocked", "the file is larger than the limit")
                while chunk := r.read(1 << 20):
                    if cancel is not None and cancel.is_set():
                        raise RuntimeError_("cancelled")
                    done += len(chunk)
                    if done > max_bytes:
                        raise RuntimeError_("blocked", "the file is larger than the limit")
                    h.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(done, total)
            if sha256 and h.hexdigest() != sha256.lower():
                raise RuntimeError_("server", "the downloaded file does not match its checksum")
            os.replace(part, final)
        except (OSError, ValueError) as e:
            raise RuntimeError_("not_connected", type(e).__name__) from None
        finally:
            if part.exists():
                part.unlink(missing_ok=True)
        return final

    def complete(self, model, system, user, schema, cancel=None, timeout=None, max_tokens=1500, device: str = "auto") -> ProviderReply:  # (the device of llama-server is chosen when it is started)
        started = time.monotonic()
        body: dict = {"model": model, "stream": True, "temperature": 0, "max_tokens": max_tokens, "stream_options": {"include_usage": True}, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        deadline = started + (timeout or self.timeout)
        for attempt in (1, 2):
            text, tin, tout = [], 0, 0
            try:
                for line in self._stream("/v1/chat/completions", body, cancel, deadline):
                    s = line.decode("utf-8", "replace").strip()
                    if not s.startswith("data:") or s == "data: [DONE]":
                        continue
                    d = json.loads(s[5:])
                    for ch in d.get("choices") or []:
                        text.append((ch.get("delta") or {}).get("content") or "")
                    u = d.get("usage") or {}
                    tin, tout = int(u.get("prompt_tokens") or tin), int(u.get("completion_tokens") or tout)
                return ProviderReply(text="".join(text), tokens_in=tin, tokens_out=tout, seconds=time.monotonic() - started, model=model, attempts=attempt)
            except RuntimeError_ as e:
                if e.kind == "bad_request" and attempt == 1 and "response_format" in body:
                    body.pop("response_format")  # the same model, asked for JSON in words only
                    continue
                return ProviderReply(failure=f"provider:{e.kind}", seconds=time.monotonic() - started, model=model, attempts=attempt)
            except ValueError:
                return ProviderReply(failure="provider:server", seconds=time.monotonic() - started, model=model, attempts=attempt)
        return ProviderReply(failure="provider:server", model=model)  # pragma: no cover
