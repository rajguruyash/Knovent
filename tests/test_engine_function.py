"""The engine as a Vercel Python function (web/api/engine.py): what it answers, what it refuses, and that the vendored copy it runs from is the real engine."""
import importlib.util
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SECRET = "test-engine-secret-0123456789abcdef"


def load_adapter(monkeypatch, env: dict):
    for k in ("DATABASE_URL", "ENGINE_SERVICE_SECRET", "EMBEDDING_MODEL_DIR", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "LLM_CREDENTIAL_KEYS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location("engine_function_under_test", ROOT / "web" / "api" / "engine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def serve():
    servers = []

    def start(handler):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_port}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def call(url, method="GET", body=None, auth=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None))
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {auth}"} if auth else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_route_path_accepts_the_original_path_or_the_rewrite(monkeypatch):
    m = load_adapter(monkeypatch, {})
    assert m.route_path("/api/engine/v1/respond") == "/v1/respond"
    assert m.route_path("/api/engine?p=/v1/ai/consent/get") == "/v1/ai/consent/get"
    assert m.route_path("/api/engine/v1/history?x=1") == "/v1/history"
    assert m.route_path("/api/engine/health") == "/health"
    assert m.route_path("/api/engine") == "/" and m.route_path("/v1/respond") == "/v1/respond"


def test_a_missing_setting_gives_a_plain_503_that_names_it_and_leaks_nothing(monkeypatch, serve):
    m = load_adapter(monkeypatch, {"ENGINE_SERVICE_SECRET": SECRET})
    base = serve(m.handler)
    for method in ("GET", "POST"):
        status, body = call(base + "/api/engine/v1/respond", method, {"x": 1} if method == "POST" else None)
        assert status == 503 and "DATABASE_URL" in body["detail"] and SECRET not in json.dumps(body)


def test_the_engine_starts_from_the_vendored_copy_and_keeps_its_shared_secret(monkeypatch, serve):
    m = load_adapter(monkeypatch, {"ENGINE_SERVICE_SECRET": SECRET, "DATABASE_URL": "postgresql://nobody:nothing@127.0.0.1:1/none"})
    base = serve(m.handler)
    status, body = call(base + "/api/engine/health")
    assert status == 200 and body["ok"] and body["dim"] == 384
    assert call(base + "/api/engine/v1/usage/get", "POST", {"business_id": "x"})[0] == 401  # no secret: refused before anything else
    assert call(base + "/api/engine/v1/usage/get", "POST", {"business_id": "x"}, auth="wrong-secret-0123456789abcdef")[0] == 401
    assert call(base + "/api/engine?p=/v1/usage/get", "POST", {"business_id": "x"})[0] == 401  # the same, by the rewritten address
    assert call(base + "/api/engine/v1/nothing", "POST", {"a": 1}, auth=SECRET)[0] == 404
    assert call(base + "/api/engine/v1/usage/get", "POST", raw=b"not json", auth=SECRET)[0] == 400
    status, body = call(base + "/api/engine/v1/usage/get", "POST", {"business_id": "not-a-uuid", "user_id": "x"}, auth=SECRET)
    assert status in (400, 403, 404, 422)  # an id that is not a UUID never reaches the database


def test_the_copy_the_function_runs_from_is_exactly_the_engine_in_src():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "vendor_engine.py"), "--check"], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr  # fix with: python scripts/vendor_engine.py


def test_vercel_routes_the_engine_path_to_the_python_function_before_the_site_rewrites():
    v = json.loads((ROOT / "web" / "vercel.json").read_text())
    assert v["rewrites"][0] == {"source": "/api/engine/:path*", "destination": "/api/engine?p=/:path*"}  # first: /api/engine/v1/x is three segments, which the site's own rewrite would take
    assert "api/engine.py" in v["functions"] and v["functions"]["api/engine.py"]["includeFiles"] == "engine/**"
    assert (ROOT / "web" / "requirements.txt").exists()
