"""The installer's steps with a temporary home folder, a recording stand-in for launchctl and a real companion on a test port. Nothing here touches the real login items, Ollama or the network."""
import io
import plistlib
import sys
import threading
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from faqengine.companion import auth, installer
from faqengine.companion.server import Companion, make_handler

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="the installer is for macOS")
ORIGIN = "https://my-site.vercel.app"


class Yes(installer.UI):
    def __init__(self, answers=None):
        self.asked, self.answers, self.said = [], answers or {}, []

    def confirm(self, title, text, ok="Continue", cancel="Cancel"):
        self.asked.append(title)
        return self.answers.get(title, True)

    def progress(self, text):
        self.said.append(text)


class Launchctl:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, cmd, **kw):
        _restart_if_registering(cmd)
        self.calls.append(cmd[:3])
        rc = 1 if (self.fail and cmd[1] in ("bootstrap", "load")) else 0
        return type("R", (), {"returncode": rc, "stdout": "", "stderr": "Bootstrap failed: 5: Input/output error" if rc else ""})()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("COMPANION_CONFIG", str(tmp_path / ".config" / "faq-engine" / "companion.json"))  # inside the fake home: never the real one
    monkeypatch.setattr(installer, "SETTLE", 0.0)
    monkeypatch.setattr(installer, "APP_DIR", tmp_path / "Library" / "Application Support" / "Knovent")
    monkeypatch.setattr(installer, "find_ollama", lambda *a, **k: "/x/ollama")
    return tmp_path


_UP: dict = {}


def _restart_if_registering(cmd):
    """A real login service that is registered starts a NEW process: it answers with a new instance id. The fake does the same, so the installer's check that the new one answers is exercised."""
    if _UP.get("comp") is not None and any(w in cmd for w in ("bootstrap", "load", "/Run", "enable")):
        _UP["comp"].instance_id = __import__("uuid").uuid4().hex[:16]


@pytest.fixture()
def companion_up(tmp_path):
    comp = Companion(auth.Pairings(tmp_path / "c.json"), runtimes={}, supervisor=None)
    _UP["comp"] = comp
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(comp, 0))
    port = httpd.server_port
    httpd.RequestHandlerClass = make_handler(comp, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield port
    httpd.shutdown()
    httpd.server_close()


def test_a_full_install_registers_a_login_service_that_trusts_only_the_given_site_and_checks_health(home, companion_up):
    lc, ui = Launchctl(), Yes()
    res = installer.install([ORIGIN], port=companion_up, ui=ui, program_args=["/x/knovent-companion"], run=lc, health_wait=3)
    assert res.ok, res.summary()
    plist = plistlib.loads((home / "Library" / "LaunchAgents" / "ai.knovent.companion.plist").read_bytes())
    assert plist["RunAtLoad"] and plist["KeepAlive"] and plist["ProgramArguments"] == ["/x/knovent-companion", "run", "--port", str(companion_up), "--replace"]  # no address on the command line
    import json
    saved = json.loads((home / ".config" / "faq-engine" / "trusted_origins.json").read_text())  # it is in the persisted file, which was written BEFORE the service was registered
    assert saved["production"] == [ORIGIN] and saved["development"] == []
    assert [c[1] for c in lc.calls][:2] == ["bootout", "bootstrap"] and lc.calls[-1][1] == "list"  # an earlier install is replaced first, never doubled; then it is checked that the service really is loaded
    assert [s.name for s in res.steps] == ["install the companion", "register the website address", "register the login service", "health check", "AI runtime"]
    assert ui.asked == ["Set up local AI on this Mac?"]  # nothing was asked about a download: the runtime was already there


def test_cancelling_the_first_question_changes_nothing(home):
    res = installer.install([ORIGIN], ui=Yes({"Set up local AI on this Mac?": False}), program_args=["/x/c"], run=Launchctl())
    assert not res.ok and "Nothing was changed" in res.error
    assert not (home / "Library" / "LaunchAgents").exists()


def test_a_build_without_a_valid_website_address_is_refused(home):
    for bad in ([], ["not an address"], ["https://ok.example", "javascript:alert(1)"]):
        res = installer.install(bad, ui=Yes(), program_args=["/x/c"], run=Launchctl())
        assert not res.ok and "website address" in res.steps[0].name


def test_a_login_service_macos_refuses_gives_an_actionable_message(home):
    res = installer.install([ORIGIN], ui=Yes(), program_args=["/x/c"], run=Launchctl(fail=True))
    assert not res.ok and "Login Items" in res.error


def test_a_companion_that_does_not_answer_says_what_to_do(home):
    res = installer.install([ORIGIN], port=1, ui=Yes(), program_args=["/x/c"], run=Launchctl(), health_wait=0.5)
    assert not res.ok and "knovent-companion.log" in res.error


def test_a_missing_runtime_is_installed_automatically_under_the_one_approval(home, companion_up, monkeypatch):
    monkeypatch.setattr(installer, "find_ollama", lambda *a, **k: None)
    calls, ui = [], Yes()
    res = installer.install([ORIGIN], port=companion_up, ui=ui, program_args=["/x/c"], run=Launchctl(), health_wait=3, ollama_installer=lambda u: calls.append(1) or (True, "Ollama installed"))
    assert res.ok and calls == [1] and ui.asked == ["Set up local AI on this Mac?"]  # one question; it said Ollama would be installed, and nothing was asked a second time
    declined, ui2, calls2 = Yes({"Set up local AI on this Mac?": False}), None, []
    res = installer.install([ORIGIN], port=companion_up, ui=declined, program_args=["/x/c"], run=Launchctl(), health_wait=3, ollama_installer=lambda u: calls2.append(1) or (True, "x"))
    assert not res.ok and calls2 == []  # declining installs nothing at all


def test_the_one_question_says_whether_ollama_will_be_installed(home, companion_up, monkeypatch):
    seen = []

    class Reading(Yes):
        def confirm(self, title, text, ok="Continue", cancel="Cancel"):
            seen.append(text)
            return True

    monkeypatch.setattr(installer, "find_ollama", lambda *a, **k: None)
    installer.install([ORIGIN], port=companion_up, ui=Reading(), program_args=["/x/c"], run=Launchctl(), health_wait=3, ollama_installer=lambda u: (True, "x"))
    monkeypatch.setattr(installer, "find_ollama", lambda *a, **k: "/x/ollama")
    installer.install([ORIGIN], port=companion_up, ui=Reading(), program_args=["/x/c"], run=Launchctl(), health_wait=3)
    assert "not installed yet" in seen[0] and "ollama.com" in seen[0] and "already installed" in seen[1]


def test_a_production_build_refuses_a_development_address_and_a_development_build_keeps_it_separate(home, companion_up):
    import json
    bad = installer.install(["http://localhost:3000"], port=companion_up, ui=Yes(), program_args=["/x/c"], run=Launchctl(), health_wait=1)
    assert not bad.ok and "exact https host" in bad.error
    dev = installer.install(["http://knovent.localhost:8768"], channel="development", port=companion_up, ui=Yes(), program_args=["/x/c"], run=Launchctl(), health_wait=3)
    assert dev.ok
    saved = json.loads((home / ".config" / "faq-engine" / "trusted_origins.json").read_text())
    assert saved["development"] == ["http://knovent.localhost:8768"] and saved["production"] == []  # never mixed into the production list


def test_an_upgrade_is_only_successful_when_a_new_process_answers(home, companion_up):
    # something already answers on the port (the old companion) and keeps answering: the install must not report success
    never_restarts = lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()  # noqa: E731 (registers, but no new process ever starts)
    res = installer.install([ORIGIN], port=companion_up, ui=Yes(), program_args=["/x/c"], run=never_restarts, health_wait=1.5)
    assert not res.ok and "did not answer" in res.error
    res = installer.install([ORIGIN], port=companion_up, ui=Yes(), program_args=["/x/c"], run=Launchctl(), health_wait=3)  # a new process answers: success
    assert res.ok

def _zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    b.seek(0)
    return b


class Opener:
    def __init__(self, data):
        self.data = data

    def __call__(self, url, timeout=0):
        return self.data


def test_the_runtime_download_is_refused_unless_it_is_ollamas_own_and_macos_accepts_its_signature(tmp_path):
    ui = Yes()
    ok_run = lambda cmd, **k: type("R", (), {"returncode": 0})()  # noqa: E731
    bad_run = lambda cmd, **k: type("R", (), {"returncode": 3})()  # noqa: E731
    assert installer.install_ollama(ui, url="https://evil.example/Ollama.zip", dest=tmp_path)[0] is False
    assert installer.install_ollama(ui, url="http://ollama.com/download/Ollama-darwin.zip", dest=tmp_path)[0] is False
    good = _zip({"Ollama.app/Contents/Info.plist": "x"})
    ok, detail = installer.install_ollama(ui, dest=tmp_path / "a", run=ok_run, opener=Opener(good))
    assert ok and (tmp_path / "a" / "Ollama.app" / "Contents" / "Info.plist").exists()
    assert installer.install_ollama(ui, dest=tmp_path / "b", run=bad_run, opener=Opener(_zip({"Ollama.app/x": "x"})))[0] is False  # signature not accepted
    assert installer.install_ollama(ui, dest=tmp_path / "c", run=ok_run, opener=Opener(_zip({"../evil": "x"})))[0] is False  # unsafe path
    assert not (tmp_path / "c").exists() and not (tmp_path / "evil").exists()


def test_uninstall_removes_the_service_and_leaves_ollama_and_models(home):
    installer.install([ORIGIN], port=1, ui=Yes(), program_args=["/x/c"], run=Launchctl(), health_wait=0.2)
    assert (home / "Library" / "LaunchAgents" / "ai.knovent.companion.plist").exists()
    lc = Launchctl()
    res = installer.uninstall(run=lc)
    assert res.ok and not (home / "Library" / "LaunchAgents" / "ai.knovent.companion.plist").exists() and lc.calls[0][1] == "bootout"
    assert "left in place" in res.steps[-1].detail


def test_a_bundle_is_installed_whole_and_the_login_service_points_into_it(home, tmp_path, monkeypatch):
    bundle = tmp_path / "dl" / "Knovent Companion.app"
    exe = bundle / "Contents" / "MacOS" / "Knovent Companion"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    (bundle / "Contents" / "Frameworks").mkdir()
    (bundle / "Contents" / "Frameworks" / "libpython.dylib").write_text("lib")
    assert installer.bundle_of(exe) == bundle and installer.bundle_of(tmp_path / "plain") is None
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[0])
        if cmd[0] == "ditto":  # what ditto does, for the test
            import shutil
            shutil.copytree(cmd[2], cmd[3], symlinks=True)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(installer.sys, "executable", str(exe), raising=False)
    monkeypatch.setattr(installer.sys, "frozen", True, raising=False)
    dest = installer._install_program(installer.APP_DIR, run)
    assert dest == installer.APP_DIR / "Knovent Companion.app" / "Contents" / "MacOS" / "Knovent Companion"
    assert (installer.APP_DIR / "Knovent Companion.app" / "Contents" / "Frameworks" / "libpython.dylib").exists()  # the libraries came with it
    assert calls[0] == "ditto" and not (installer.APP_DIR / "Knovent Companion.app.new").exists()
    again = installer._install_program(installer.APP_DIR, run)  # an upgrade replaces the bundle, it does not nest or pile up
    assert again == dest and len(list(installer.APP_DIR.iterdir())) == 1


def test_an_upgrade_retries_until_the_service_is_really_loaded(home, companion_up):
    class Flaky(Launchctl):
        """The first registration looks fine to the commands but leaves nothing loaded (what macOS did when bootstrap followed bootout too soon)."""
        def __init__(self):
            super().__init__()
            self.listed = 0

        def __call__(self, cmd, **kw):
            r = super().__call__(cmd, **kw)
            if cmd[1] == "list":
                self.listed += 1
                r.returncode = 1 if self.listed == 1 else 0
            return r

    lc = Flaky()
    res = installer.install([ORIGIN], port=companion_up, ui=Yes(), program_args=["/x/c"], run=lc, health_wait=3)
    assert res.ok, res.summary()
    assert [c[1] for c in lc.calls].count("bootstrap") == 2  # it registered again
    never = Launchctl()
    never.__call__ = None  # noqa: B010

    class Never(Launchctl):
        def __call__(self, cmd, **kw):
            r = super().__call__(cmd, **kw)
            if cmd[1] == "list":
                r.returncode = 1
            return r

    res = installer.install([ORIGIN], port=companion_up, ui=Yes(), program_args=["/x/c"], run=Never(), health_wait=0.5)
    assert not res.ok and "login service" in res.steps[-1].name  # never loaded: said plainly, not reported as done


def test_a_production_install_trusts_exactly_its_own_website_and_drops_leftover_test_addresses(home, companion_up):
    from faqengine.companion.origins import TrustedOrigins

    TrustedOrigins().set_development(["http://knovent.localhost:8768"])  # left by an earlier developer build
    TrustedOrigins().set_production(["https://old-site.vercel.app"])
    res = installer.install([ORIGIN], port=companion_up, ui=Yes(), program_args=["/x/knovent-companion"], run=Launchctl(), health_wait=3)
    assert res.ok, res.summary()
    reg = TrustedOrigins()
    assert reg.production == {ORIGIN} and reg.development == set()
