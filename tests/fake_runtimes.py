"""Stand-ins for the local runtimes, over real HTTP on loopback: Ollama's native streaming API and llama-server's OpenAI-compatible SSE. They stream slowly on request so that cancellation can be
tested for real, and they record when a client hung up mid-answer (which is what stops a real runtime generating)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeRuntime:
    def __init__(self, kind="ollama", answer="OK", models=("qwen3:4b",), chunk_delay=0.0, chunks=None, reject=()):
        self.kind, self.answer, self.models = kind, answer, list(models)
        self.chunk_delay, self.chunks = chunk_delay, chunks  # chunks: how many pieces the answer is cut into (None: one per word)
        self.reject = set(reject)
        self.sizes: dict[str, int] = {}  # bytes each installed model takes (default 1 GB)
        self.digests: dict[str, str] = {}
        self.pull_digest: str | None = None  # what a pull leaves a model's digest as (None: unchanged)
        self.pull_layers: list[tuple[str, int]] = []  # (digest, bytes) per layer the pull reports; empty: one layer of 100
        self.loaded: list[str] = []
        self.deleted: list[str] = []
        self.requests: list[dict] = []
        self.hung_up = threading.Event()
        self.completed = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._json(200, {"version": "0.0-test"})
                if self.path == "/api/tags":
                    return self._json(200, {"models": [{"name": m, "size": outer.sizes.get(m, 10**9), "digest": outer.digests.get(m, "sha256:" + "ab" * 32), "modified_at": "2026-11-02T10:00:00Z"} for m in outer.models]})
                if self.path == "/api/ps":
                    return self._json(200, {"models": [{"name": m} for m in outer.loaded]})
                if self.path == "/health":
                    return self._json(200, {"status": "ok"})
                if self.path == "/v1/models":
                    return self._json(200, {"data": [{"id": m} for m in outer.models]})
                self._json(404, {"error": "no"})

            def do_DELETE(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                if body.get("model") in outer.models:
                    outer.models.remove(body["model"])
                    outer.deleted.append(body["model"])
                self._json(200, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                outer.requests.append({"path": self.path, "body": body})
                if self.path == "/api/pull":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    if outer.pull_layers:  # like the real one: the big layer first, then small ones that each start again from zero
                        for digest, total in outer.pull_layers:
                            for done in (0, total // 2, total):
                                self.wfile.write((json.dumps({"status": f"pulling {digest[:12]}", "digest": digest, "completed": done, "total": total}) + "\n").encode())
                                self.wfile.flush()
                    else:
                        for done in (10, 50, 100):
                            self.wfile.write((json.dumps({"status": "pulling", "completed": done, "total": 100}) + "\n").encode())
                            self.wfile.flush()
                    if body.get("model") not in outer.models:
                        outer.models.append(body["model"])
                    if outer.pull_digest:
                        outer.digests[body["model"]] = outer.pull_digest
                    return
                if body.get("model") not in outer.models:
                    return self._json(404, {"error": f"model '{body.get('model')}' not found"})
                if "response_format" in body and "response_format" in outer.reject:
                    return self._json(400, {"error": {"message": "unsupported parameter: response_format"}})
                words = outer.answer.split(" ")
                pieces = [" ".join(words[i::outer.chunks]) for i in range(outer.chunks)] if outer.chunks else [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson" if outer.kind == "ollama" else "text/event-stream")
                self.end_headers()
                try:
                    for i, piece in enumerate(pieces):
                        if outer.kind == "ollama":
                            line = {"message": {"role": "assistant", "content": piece}, "done": False}
                            self.wfile.write((json.dumps(line) + "\n").encode())
                        else:
                            self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n").encode())
                        self.wfile.flush()
                        time.sleep(outer.chunk_delay)
                    if outer.kind == "ollama":
                        self.wfile.write((json.dumps({"message": {"content": ""}, "done": True, "prompt_eval_count": 21, "eval_count": 9}) + "\n").encode())
                    else:
                        self.wfile.write(("data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 21, "completion_tokens": 9}}) + "\n\ndata: [DONE]\n\n").encode())
                    self.wfile.flush()
                    outer.completed += 1
                except (BrokenPipeError, ConnectionResetError, OSError):
                    outer.hung_up.set()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
