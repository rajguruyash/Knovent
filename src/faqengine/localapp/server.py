"""The engine's small internal HTTP service for the one-command app. It listens on 127.0.0.1 only and answers only requests that carry this run's random secret (the website's server is the one
caller), so no other program or web page can read a business's knowledge through it."""
from __future__ import annotations

import base64
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import EngineError, LocalEngine
from .training import Trainer

log = logging.getLogger("knovent.engine")
MAX_BODY = 40 * 1024 * 1024


def make_handler(engine: LocalEngine, secret: str):
    trainer = Trainer(engine)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # no access log: it would carry questions
            return

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                self.rfile.read(0)
                return self._send(413, {"error": "too large"})
            raw = self.rfile.read(n)  # always read the body, or the next request on this connection is corrupted
            if not hmac.compare_digest(self.headers.get("X-Knovent-Secret", ""), secret):
                return self._send(403, {"error": "not allowed"})
            try:
                b = json.loads(raw or b"{}")
                if self.path == "/ingest":
                    out = engine.ingest(b.get("business_id"), b.get("source_id"), b.get("filename", ""), base64.b64decode(b.get("data_b64", "")))
                elif self.path == "/ingest_url":
                    out = engine.ingest_url(b.get("business_id"), b.get("source_id"), str(b.get("url") or ""), bool(b.get("whole")))
                elif self.path == "/remove":
                    out = engine.remove(b.get("business_id"), b.get("source_id"))
                elif self.path == "/respond":
                    out = engine.respond(b.get("business_id"), b.get("query", ""), b.get("model") or None, "chat" if b.get("style") == "chat" else "plain")
                elif self.path == "/train/start":
                    out = trainer.start(b.get("business_id"), str(b.get("name") or "business")[:60])
                elif self.path == "/train/status":
                    out = trainer.status(b.get("business_id"))
                elif self.path == "/health":
                    out = {"ok": True}
                else:
                    return self._send(404, {"error": "not found"})
                self._send(200, out)
            except EngineError as e:
                self._send(e.status, {"error": e.message})
            except Exception:  # noqa: BLE001
                log.exception("engine request failed")
                self._send(500, {"error": "the engine could not do that"})

        do_GET = do_POST  # noqa: N815

    return H


def serve(engine: LocalEngine, secret: str, port: int = 8769) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, secret))
    httpd.daemon_threads = True
    return httpd
