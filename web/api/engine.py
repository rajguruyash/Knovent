"""The knowledge engine as a Vercel Python function: the SAME service code that runs in a container (faqengine.hosted.service), behind the same shared secret, so the site needs no second host.

The browser never calls this. The site's own functions (api/_routes/*.js) authenticate the person and then call `<ENGINE_URL>/v1/...` with `Authorization: Bearer <ENGINE_SERVICE_SECRET>`;
this file answers them. vercel.json rewrites /api/engine/<anything> to this one function. The copy of the engine in engine/ is made by scripts/vendor_engine.py (a test keeps it identical to src/).

Set in the Vercel project (server only): ENGINE_SERVICE_SECRET (at least 24 random characters, the same value the site's functions use), DATABASE_URL (Supabase's pooled connection string),
SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (to read uploaded files), LLM_CREDENTIAL_KEYS (only to store a business's own provider key). If something is missing the function answers 503 and says what.
State that a long-running container keeps (each business's search index, request rate limits) lives only as long as a function instance does here; correctness never depends on it."""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent.parent / "engine"
sys.path.insert(0, str(HERE))
os.environ.setdefault("EMBEDDING_MODEL_DIR", str(HERE / "models" / "minilm-l6" / "model"))
PREFIX = "/api/engine"


def route_path(raw: str) -> str:
    """The service's own path (/v1/respond) from whatever Vercel hands over: the original /api/engine/v1/respond, or the rewrite's ?p=/v1/respond."""
    parts = urlsplit(raw)
    given = (parse_qs(parts.query).get("p") or [""])[0]
    path = given or parts.path
    if path.startswith(PREFIX):
        path = path[len(PREFIX):]
    return "/" + path.lstrip("/") if path.strip("/") else "/"


def _failed(why: str) -> type:
    class handler(BaseHTTPRequestHandler):  # the engine could not start: every request says why, in plain words and without any secret
        def _say(self):
            data = json.dumps({"error": "the knowledge engine is not set up yet", "detail": why}).encode()
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _say

        def log_message(self, *a):
            pass

    return handler


def build():
    try:
        from faqengine.hosted.service import from_env, make_handler
        engine, secret = from_env()
    except SystemExit as e:  # from_env reports a missing setting by exiting; a function must answer instead
        return _failed(str(e.code) if e.code else "a setting is missing")
    except Exception as e:  # noqa: BLE001 - the type only: a message may carry a connection string
        return _failed(f"it could not start ({type(e).__name__})")
    base = make_handler(engine, secret)

    class handler(base):  # type: ignore[valid-type, misc]
        def do_GET(self):
            self.path = route_path(self.path)
            return super().do_GET()

        def do_POST(self):
            self.path = route_path(self.path)
            return super().do_POST()

    return handler


handler = build()
