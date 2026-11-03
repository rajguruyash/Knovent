"""Which websites may pair without a code, and that there is only ever one companion. The trusted-origin file is validated, persisted, separated into production and development, and re-read while the companion
runs; the lock file keeps a second companion from fighting for the port and lets the installed service replace the first."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from faqengine.companion import auth
from faqengine.companion.instance import Instance
from faqengine.companion.origins import TrustedOrigins, validate_origin
from faqengine.companion.server import Companion, make_handler

SITE = "https://knovent-web.vercel.app"


@pytest.mark.parametrize("bad", ["http://knovent-web.vercel.app", "https://*.vercel.app", "https://knovent-web.vercel.app/app", "https://user@knovent-web.vercel.app", "https://localhost", "https://127.0.0.1",
                                 "https://10.0.0.5", "https://knovent web.vercel.app", "knovent-web.vercel.app", "https://evil.com#@knovent-web.vercel.app", "https://a..b.com", "", None, 5])
def test_a_production_origin_is_an_exact_https_host_and_nothing_else(bad):
    with pytest.raises(ValueError):
        validate_origin(bad)


def test_a_production_origin_is_made_canonical_and_a_development_origin_is_a_separate_choice():
    assert validate_origin("HTTPS://Knovent-Web.Vercel.App/") == SITE
    assert validate_origin("https://app.example.com:8443") == "https://app.example.com:8443"
    for dev in ("http://localhost:3000", "http://127.0.0.1:8768", "http://knovent.localhost:8768"):
        assert validate_origin(dev, development=True) == dev
        with pytest.raises(ValueError):
            validate_origin(dev)  # never accepted as a production origin
    with pytest.raises(ValueError):
        validate_origin("http://evil.com", development=True)  # development is not "any http address"


def test_origins_persist_and_the_two_lists_stay_apart(tmp_path):
    f = tmp_path / "trusted_origins.json"
    t = TrustedOrigins(f)
    assert SITE not in t and not t
    t.set_production([SITE])
    t.set_development(["http://knovent.localhost:8768"])
    again = TrustedOrigins(f)  # a new process (a restart, an upgrade) finds them
    assert SITE in again and "http://knovent.localhost:8768" in again and json.loads(f.read_text())["schema"] == 1
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        t.set_production([SITE, "http://localhost:3000"])  # one bad entry changes nothing
    assert TrustedOrigins(f).production == {SITE}


def test_a_damaged_or_unknown_file_trusts_nothing_and_a_command_line_origin_is_never_saved(tmp_path):
    f = tmp_path / "trusted_origins.json"
    f.write_text("{not json")
    assert SITE not in TrustedOrigins(f)
    f.write_text(json.dumps({"schema": 99, "production": [SITE]}))
    assert SITE not in TrustedOrigins(f)
    f.write_text(json.dumps({"schema": 1, "production": [SITE, "https://*.evil.com", "http://localhost"], "development": []}))
    t = TrustedOrigins(f)
    assert t.production == {SITE} and len(t.warnings) == 2  # the bad entries are dropped and reported, the good one stays
    s = TrustedOrigins(tmp_path / "other.json", session=["http://knovent.localhost:8768"])
    assert "http://knovent.localhost:8768" in s and not (tmp_path / "other.json").exists()
    with pytest.raises(ValueError):
        TrustedOrigins(tmp_path / "x.json", session=["http://evil.com"])


@pytest.fixture()
def served(tmp_path):
    origins = TrustedOrigins(tmp_path / "trusted_origins.json", check_every=0.0)
    comp = Companion(auth.Pairings(tmp_path / "c.json"), runtimes={}, supervisor=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(comp, 0, origins))
    port = httpd.server_port
    httpd.RequestHandlerClass = make_handler(comp, port, origins)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port, origins
    httpd.shutdown()
    httpd.server_close()


def pair(port, origin):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/pair", data=json.dumps({"label": "t", "business": "11111111-aaaa-bbbb-cccc-000000000001"}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Origin": origin})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_a_new_site_address_takes_effect_while_the_companion_runs(served):
    port, origins = served
    assert pair(port, SITE) == (401, {"error": "origin_not_trusted"})
    origins.set_production([SITE])  # what the installer or an upgrade does: no restart of the companion
    st, body = pair(port, SITE)
    assert st == 200 and body["bound"] is True
    assert pair(port, "https://evil.example")[1] == {"error": "origin_not_trusted"}  # nothing else became trusted
    origins.set_production([])
    assert pair(port, SITE)[0] == 401  # and taking it away takes effect too


def test_a_second_companion_leaves_and_the_installed_service_replaces_the_first(tmp_path):
    lock = tmp_path / "companion-47890.lock"
    stopped, alive = [], {4242: True}
    first = Instance(lock, alive=lambda p: alive.get(p, False), command=lambda p: "/x/Knovent Companion run", stop=lambda p: (stopped.append(p), alive.__setitem__(p, False)), pid=4242)
    assert first.acquire() == (True, "")
    manual = Instance(lock, alive=lambda p: alive.get(p, False), command=lambda p: "/x/Knovent Companion run", stop=first.stop, pid=5000)
    ok, why = manual.acquire(replace=False)
    assert ok is False and "already running" in why and stopped == []  # a copy started by hand never fights for the port
    service = Instance(lock, alive=lambda p: alive.get(p, False), command=lambda p: "/x/Knovent Companion run", stop=first.stop, pid=6000)
    assert service.acquire(replace=True, wait=2) == (True, "") and stopped == [4242]  # the installed service replaces it
    assert json.loads(lock.read_text())["pid"] == 6000
    service.release()
    assert not lock.exists()


def test_only_a_knovent_process_named_in_our_lock_file_is_ever_stopped(tmp_path):
    lock = tmp_path / "companion-47890.lock"
    lock.write_text(json.dumps({"pid": 777, "started": 0, "program": "x"}))
    stopped = []
    unrelated = Instance(lock, alive=lambda p: True, command=lambda p: "/usr/bin/some-other-program --serve", stop=lambda p: stopped.append(p), pid=900)  # the id was reused by something else
    assert unrelated.holder() is None and unrelated.acquire(replace=True) == (True, "") and stopped == []
    dead = Instance(tmp_path / "d.lock", alive=lambda p: False, command=lambda p: "knovent", pid=1)
    (tmp_path / "d.lock").write_text(json.dumps({"pid": 5}))
    assert dead.holder() is None  # a dead process holds nothing
    assert Instance(port=47890).path != Instance(port=47895).path  # one lock per port: a test companion cannot replace the real one


def test_a_real_second_companion_process_does_not_start(tmp_path):
    env = {**os.environ, "COMPANION_CONFIG": str(tmp_path / "companion.json"), "KNOVENT_NO_RUNTIME": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    port = 47883
    cmd = [sys.executable, "-m", "faqengine.cli", "companion", "run", "--port", str(port)]
    first = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/hello", timeout=1).close()
                break
            except OSError:
                time.sleep(0.25)
        else:
            pytest.fail("the first companion did not start")
        second = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        assert second.returncode == 0 and "already running" in second.stdout  # it left, and left the first alone
        urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/hello", timeout=2).close()
    finally:
        first.terminate()
        first.wait(timeout=10)
