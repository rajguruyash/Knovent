"""Fixtures shared by the slow website tests."""
import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import pytest


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    from faqengine.studio import server
    from faqengine.studio.store import Store

    store = Store(tmp_path_factory.mktemp("studio"))
    server.Handler.app = server.App(store)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    server.Handler.port = httpd.server_port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"

    def call(method, path, body=None):
        req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def j(method, path, body=None):
        return json.loads(call(method, path, body))

    def wait(pid, key, done, limit=420):
        end = time.time() + limit
        while time.time() < end:
            state = j("GET", f"/api/projects/{pid}?light=1")[key]["state"]
            if state in done:
                return state
            time.sleep(1)
        raise TimeoutError(f"{key} did not finish in {limit}s")

    yield type("Site", (), {"j": staticmethod(j), "call": staticmethod(call), "wait": staticmethod(wait), "store": store})
    httpd.shutdown()


def _para(topic: str, n: int = 12) -> str:
    return " ".join(f"{topic} is explained here in plain words, number {i}." for i in range(n))


@pytest.fixture(scope="module")
def webshop():
    """A small web site served from this computer: a home page that links to others, a robots.txt, an empty (JavaScript-only) page, a
    duplicate, a redirect, a plain-text page, a PDF and an oversized page. Yields its address."""
    from http.server import BaseHTTPRequestHandler

    home = ('<html><head><title>Bike Shop</title></head><body><nav><a href="/about">Menu: about us</a></nav><main><h1>Welcome to the Bike Shop</h1>'
            f"<p>{_para('Our shop')}</p>"
            '<a href="/faq">FAQ</a> <a href="/warranty#top">Warranty</a> <a href="/secret">Secret</a> <a href="/login">Log in</a> <a href="/logo.png">Logo</a> '
            '<a href="/empty">Empty</a> <a href="/dupe">Same as the FAQ</a> <a href="/redirect">Moved</a> <a href="http://other.example/page">Another site</a> <a href="mailto:a@b.c">Mail</a>'
            "</main><footer>Copyright 2026 Bike Shop Ltd. All rights reserved. Privacy and cookies.</footer></body></html>")
    faq = ('<html><head><title>Frequently asked questions</title></head><body><nav>Menu</nav><article><h1>Frequently asked questions</h1>'
           f"<h2>How long is the frame warranty?</h2><p>All new bikes carry a two year frame warranty. {_para('The warranty')}</p>"
           f"<h2>Do you repair bikes bought elsewhere?</h2><p>Yes, we repair any bike. {_para('Repairs')}</p></article>"
           "<footer>Copyright 2026 Bike Shop Ltd.</footer></body></html>")
    warranty = f'<html><head><title>Warranty</title></head><body><main><h1>Warranty</h1><p>{_para("Warranty cover", 14)}</p></main></body></html>'
    pages = {
        "/": ("text/html", home), "/faq": ("text/html", faq), "/dupe": ("text/html", faq), "/warranty": ("text/html", warranty), "/secret": ("text/html", warranty),
        "/login": ("text/html", warranty), "/empty": ("text/html", '<html><head><title>App</title></head><body><div id="app"></div><script>render()</script></body></html>'),
        "/plain": ("text/plain", _para("Plain text", 10).replace(". ", ".\n\n")), "/pdf": ("application/pdf", "%PDF-1.4"),
        "/big": ("text/html", "<p>" + "word " * 1_300_000 + "</p>"), "/robots.txt": ("text/plain", "User-agent: *\nDisallow: /secret\n"),
    }

    class Site(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/faq")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            if path == "/redirect-ftp":
                self.send_response(302)
                self.send_header("Location", "ftp://example.com/file")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            kind, body = pages.get(path, (None, None))
            if body is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                return self.end_headers()
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", kind + ("; charset=utf-8" if kind.startswith("text") else ""))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def verified_disclosure_entries() -> dict:
    """Every provider entry with every item of the verification filled in, with obviously fake values (example.invalid): a TEST DOUBLE for a provider whose terms were verified, to prove what happens
    once one is. Nothing here is a statement about any real provider."""
    from faqengine.hosted import disclosure

    return {k: {**v, "terms_url": f"https://terms.example.invalid/{k}", "service_tier": "test tier", "retention": "test double: no real provider is described here",
                "training_use": "test double: no real provider is described here", "abuse_monitoring": "test double: no real provider is described here",
                "contractual_commitments": "test double: no real provider is described here", "verified_on": "2026-11-02", "verified_by": "tests"} for k, v in disclosure.PROVIDERS.items()}


@pytest.fixture()
def verified_providers(tmp_path, monkeypatch):
    f = tmp_path / "disclosure.json"
    f.write_text(json.dumps(verified_disclosure_entries()))
    monkeypatch.setenv("AI_DISCLOSURE_FILE", str(f))
    return f
