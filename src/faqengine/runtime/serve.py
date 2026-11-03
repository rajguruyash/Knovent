"""Tiny HTTP server around the engine (standard library only).

    python serve.py --package ..            # http://127.0.0.1:8080
    FAQENGINE_API_KEY=secret python serve.py --package .. --host 0.0.0.0

    curl -s localhost:8080/answer   -d '{"query": "how long does a transfer take?"}'
    curl -s localhost:8080/respond  -d '{"query": "what time do you close on sunday?"}'   # outcome: faq | grounded | insufficient | conflict
    curl -s localhost:8080/retrieve -d '{"query": "how long does a transfer take?", "k": 5}'

    # teach it: this question belongs to that entry (searched at once, kept across restarts, trained on at the next ci/improve.py retrain)
    curl -s localhost:8080/feedback -d '{"question": "can I post it back", "entry_id": "returns", "verdict": "approve"}'
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engine import Engine

MAX_BODY = 64 * 1024
MAX_QUERY = 2000
LOOPBACK = ("127.0.0.1", "localhost", "::1")


class Live:
    """The engine being served. When a new version of the package is promoted (ci/improve.py) the next request loads it, with no restart."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.engine = Engine(self.root)
        self.lock = threading.Lock()

    def get(self) -> Engine:
        if self.engine.stale():
            with self.lock:
                if self.engine.stale():
                    try:
                        self.engine = Engine(self.root)
                        print("loaded the new version of the package")
                    except Exception as e:  # noqa: BLE001 - files may be half copied: keep answering with the version in memory
                        print(f"the new version is not loadable yet ({e}); still serving the previous one")
        return self.engine


def append(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **row}, ensure_ascii=False) + "\n")


def make_handler(live: Live, api_key: str | None, cors: bool, feedback_key: str | None = None, local_only: bool = True, log_questions: bool = False):
    """`api_key` guards asking; `feedback_key` (default: the same key) guards teaching. With no key at all, teaching is allowed only
    when the server listens on this computer alone (`local_only`)."""

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if cors:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self, key: str | None) -> bool:
            if not key:
                return True
            given = self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            return hmac.compare_digest(given.encode(), key.encode())

        def _may_teach(self) -> tuple[bool, str]:
            key = feedback_key or api_key
            if not key and not local_only:
                return False, "teaching is off: this server is open to the network and has no key (set FAQENGINE_API_KEY or FAQENGINE_FEEDBACK_KEY)"
            return (self._authorised(key), "missing or wrong key") if key else (True, "")

        def do_OPTIONS(self):  # CORS preflight
            self._send(204, {})

        def do_GET(self):
            engine = live.get()
            if self.path == "/health":
                self._send(200, {"ok": True, "entries": len(engine.entries), "model": engine.cfg["onnx"], "learned": sum(map(len, engine.learned.values()))})
            elif self.path == "/learned":
                ok, why = self._may_teach()
                if not ok:
                    return self._send(401 if "key" in why else 403, {"error": why})
                path = engine.state / "learned.jsonl"
                rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.is_file() else []
                self._send(200, {"learned": rows})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path not in ("/answer", "/retrieve", "/feedback", "/respond"):
                return self._send(404, {"error": "not found"})
            if self.path == "/feedback":
                ok, why = self._may_teach()
                if not ok:
                    return self._send(401 if "key" in why else 403, {"error": why})
            elif not self._authorised(api_key):
                return self._send(401, {"error": "missing or wrong API key"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    return self._send(413 if length > MAX_BODY else 400, {"error": "body must be 1 byte to 64 KB of JSON"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("not an object")
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "body is not valid JSON"})
            engine = live.get()
            if self.path == "/feedback":
                return self._feedback(engine, body)
            query = body.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
                return self._send(400, {"error": f"'query' must be a non-empty string up to {MAX_QUERY} characters"})
            k = body.get("k", 5)
            if not isinstance(k, int) or not 1 <= k <= 20:
                return self._send(400, {"error": "'k' must be an integer from 1 to 20"})
            if self.path == "/respond":  # the evidence-aware answer: stored FAQ answer, grounded passages, or an honest "not covered"
                return self._send(200, engine.respond(query))
            if self.path == "/answer":
                result = engine.answer(query, min(k, 10))
                if log_questions:  # off by default: these are your customers' words
                    append(engine.state / "asked.jsonl", {"query": query, "decision": result["decision"], "top_entry": result["hits"][0]["entry_id"], "score": result["confidence"]})
                self._send(200, result)
            else:
                self._send(200, {"hits": engine.retrieve(query, k)})

        def _feedback(self, engine: Engine, body: dict) -> None:
            verdict, question, entry_id = body.get("verdict"), body.get("question"), body.get("entry_id")
            if verdict not in ("approve", "correct", "reject") or not isinstance(question, str):
                return self._send(400, {"error": "send {\"question\": ..., \"verdict\": \"approve\" | \"correct\" | \"reject\", \"entry_id\": ...}"})
            append(engine.state / "feedback.jsonl", {"question": question[:MAX_QUERY], "verdict": verdict, "entry_id": entry_id})
            if verdict == "reject":
                return self._send(200, {"logged": True, "learned": False})
            if not isinstance(entry_id, str):
                return self._send(400, {"error": "'entry_id' (the entry this question belongs to) is needed to approve or correct"})
            try:
                self._send(200, {"logged": True, "learned": engine.learn(entry_id, question, by=verdict)["added"]})
            except ValueError as e:
                self._send(400, {"error": str(e)})

        def log_message(self, fmt, *args):  # one quiet line per request
            print(f"{self.address_string()} {fmt % args}")

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--package", default=str(Path(__file__).resolve().parents[1]), help="package folder (default: the parent of runtime/)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--cors", action="store_true", help="allow browser pages on other origins to call the API")
    ap.add_argument("--log-questions", action="store_true", help="keep every asked question in data/asked.jsonl, to find what to teach it (off by default: they are your customers' words)")
    args = ap.parse_args()
    live = Live(args.package)
    key = os.environ.get("FAQENGINE_API_KEY") or None
    teach = os.environ.get("FAQENGINE_FEEDBACK_KEY") or None
    print(f"serving {len(live.engine.entries)} entries on http://{args.host}:{args.port}" + (" (API key required)" if key else " (no API key)")
          + f"; {sum(map(len, live.engine.learned.values()))} questions learned so far")
    handler = make_handler(live, key, args.cors, teach, args.host in LOOPBACK, args.log_questions)
    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
