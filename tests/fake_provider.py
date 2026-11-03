"""A stand-in for the language-model providers' HTTP APIs (OpenAI-style, Anthropic, Gemini), so that the connection code is tested for real without a key or the internet."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class FakeProvider:
    def __init__(self, key="sk-test-key-123456", reply="OK", models=("chat-small", "chat-large", "text-embedding-3-small"), reject=()):
        self.key, self.reply, self.models = key, reply, list(models)
        self.reject = set(reject)  # parameters this "model" refuses with a 400 naming them: response_format, temperature, max_tokens, tools, responseSchema, format
        self.statuses: list[int] = []  # answered in order, one per chat request, before normal service resumes (to script an outage)
        self.requests: list[dict] = []
        self.status: int | None = None  # force every answer to this HTTP status
        self.delay = 0.0
        self.redirect = False
        self.usage = (11, 7)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _auth_ok(self):
                h = self.headers
                given = (h.get("Authorization", "").removeprefix("Bearer ") or h.get("x-api-key") or h.get("x-goog-api-key") or "")
                return given == outer.key

            def _handle(self, method):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
                outer.requests.append({"method": method, "path": self.path, "headers": dict(self.headers), "body": body})
                if outer.delay:
                    time.sleep(outer.delay)
                if outer.redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:1/elsewhere")
                    self.end_headers()
                    return
                if outer.status:
                    return self._send(outer.status, {"error": {"message": f"forced {outer.status} for key {self.headers.get('Authorization', '')}"}})
                if outer.statuses and method == "POST":
                    code = outer.statuses.pop(0)
                    if code != 200:
                        return self._send(code, {"error": {"message": f"scripted {code}"}})
                if not self._auth_ok() and not self.path.startswith("/api/"):  # (Ollama's own API takes no key)
                    return self._send(401, {"error": {"message": "Incorrect API key provided"}})
                path = urlparse(self.path).path
                reply = lambda: outer.reply(body) if callable(outer.reply) else outer.reply  # noqa: E731  (only the completion routes need one)
                if method == "POST" and isinstance(body, dict):
                    gen = body.get("generationConfig") or {}
                    named = {"response_format": "response_format" in body, "temperature": "temperature" in body or "temperature" in gen, "max_tokens": "max_tokens" in body, "tools": "tools" in body,
                             "responseSchema": "responseSchema" in gen, "format": "format" in body}
                    for param in outer.reject:
                        if named.get(param):
                            hint = " Use 'max_completion_tokens' instead." if param == "max_tokens" else ""
                            return self._send(400, {"error": {"message": f"Unsupported parameter: '{param}' is not supported with this model.{hint}"}})
                if path.endswith("/api/tags"):
                    return self._send(200, {"models": [{"name": m} for m in outer.models]})
                if path.endswith("/api/chat"):
                    return self._send(200, {"message": {"role": "assistant", "content": reply()}, "prompt_eval_count": outer.usage[0], "eval_count": outer.usage[1], "total_duration": 2_000_000_000, "load_duration": 500_000_000})
                if method == "GET" and path.endswith("/models"):
                    if "v1beta" in path:
                        return self._send(200, {"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]} for m in outer.models]})
                    return self._send(200, {"data": [{"id": m} for m in outer.models]})
                if path.endswith("/chat/completions"):
                    return self._send(200, {"choices": [{"message": {"content": reply()}}], "usage": {"prompt_tokens": outer.usage[0], "completion_tokens": outer.usage[1]}})
                if path.endswith("/messages"):
                    text = reply()
                    if "tools" in body:  # a forced tool call: the structured result comes back as the tool's input
                        try:
                            return self._send(200, {"content": [{"type": "tool_use", "name": "answer", "input": json.loads(text)}], "usage": {"input_tokens": outer.usage[0], "output_tokens": outer.usage[1]}})
                        except ValueError:
                            pass
                    return self._send(200, {"content": [{"type": "text", "text": text}], "usage": {"input_tokens": outer.usage[0], "output_tokens": outer.usage[1]}})
                if path.endswith(":generateContent"):
                    return self._send(200, {"candidates": [{"content": {"parts": [{"text": reply()}]}}], "usageMetadata": {"promptTokenCount": outer.usage[0], "candidatesTokenCount": outer.usage[1]}})
                self._send(404, {"error": {"message": "no such route"}})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, prefix="/v1") -> str:
        return f"http://127.0.0.1:{self.port}{prefix}"

    def close(self):
        self.httpd.shutdown()
