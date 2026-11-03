"""The Windows path of the shared installer, run with a FAKED Windows environment (a temporary %LOCALAPPDATA%, a recording stand-in for schtasks and PowerShell, a real companion on a test port).
This proves the logic: what is written, what is run, in what order, what is refused. It does NOT prove that Windows accepts it: that is what scripts/platform_selftest.py on a real Windows PC is for."""
import io
import threading
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from faqengine.companion import auth, installer
from faqengine.companion import platforms as P
from faqengine.companion.server import Companion, make_handler

ORIGIN = "https://my-site.vercel.app"


class Yes(installer.UI):
    def __init__(self, answers=None):
        self.asked, self.answers = [], answers or {}

    def confirm(self, title, text, ok="Continue", cancel="Cancel"):
        self.asked.append(title)
        return self.answers.get(title, True)

    def progress(self, text):
        pass


class Recorder:
    def __init__(self, results=None):
        self.calls, self.results = [], results or {}

    def __call__(self, cmd, **kw):
        _restart_if_registering(cmd)
        self.calls.append(cmd)
        rc = self.results.get(cmd[1] if len(cmd) > 1 else cmd[0], 0)
        return type("R", (), {"returncode": rc, "stdout": self.results.get("stdout", ""), "stderr": ""})()


@pytest.fixture()
def win(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "SETTLE", 0.0)
    monkeypatch.setattr(installer, "find_ollama", lambda *a, **k: "C:/ollama.exe")
    env = {"LOCALAPPDATA": str(tmp_path / "Local"), "USERNAME": "Ann", "USERDOMAIN": "PC"}
    return P.WindowsAdapter(env=env, home=tmp_path), tmp_path


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


def test_a_windows_install_writes_the_logon_task_registers_it_and_verifies_it(win, companion_up):
    adapter, home = win
    rec = Recorder()
    res = installer.install([ORIGIN], port=companion_up, ui=Yes(), program_args=[str(home / "k.exe")], run=rec, health_wait=3, adapter=adapter, version="0.1.0")
    assert res.ok, res.summary()
    xml_path = adapter.service_path("ai.knovent.companion")
    raw = xml_path.read_bytes()
    assert raw[:2] in (b"\xff\xfe", b"\xfe\xff")  # UTF-16 with a byte order mark: what schtasks /XML reads
    root = ET.fromstring(raw.decode("utf-16").split("?>", 1)[1])
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.find(".//t:LogonTrigger", ns) is not None and root.find(".//t:TimeTrigger/t:Repetition/t:Interval", ns).text == "PT5M"  # logon, plus a 5 minute safety net
    assert root.find(".//t:MultipleInstancesPolicy", ns).text == "IgnoreNew"  # the safety net never starts a second copy
    assert [c[1] for c in rec.calls[:3]] == ["/End", "/Create", "/Run"] and rec.calls[-1][1] == "/Query"
    assert root.find(".//t:Exec/t:Arguments", ns).text == f"run --port {companion_up} --replace"  # no address on the command line: the persisted file holds it


def test_a_task_scheduler_that_refuses_gives_a_plain_message(win):
    adapter, home = win
    res = installer.install([ORIGIN], ui=Yes(), program_args=["k.exe"], run=Recorder({"/Create": 1}), adapter=adapter)
    assert not res.ok and "Task Scheduler" in res.error


def test_nothing_is_installed_on_windows_when_the_person_declines(win):
    adapter, home = win
    rec = Recorder()
    res = installer.install([ORIGIN], ui=Yes({"Set up local AI on this computer?": False}), program_args=["k.exe"], run=rec, adapter=adapter)
    assert not res.ok and rec.calls == [] and not adapter.service_path("ai.knovent.companion").exists()


def test_a_missing_runtime_is_installed_on_windows_under_the_one_approval(win, companion_up, monkeypatch):
    adapter, home = win
    monkeypatch.setattr(installer, "find_ollama", lambda *a, **k: None)
    calls, ui = [], Yes()
    approved = installer.install([ORIGIN], port=companion_up, ui=ui, program_args=["k.exe"], run=Recorder(), adapter=adapter, health_wait=3, ollama_installer=lambda u: calls.append(1) or (True, "x"))
    assert approved.ok and calls == [1] and ui.asked == ["Set up local AI on this computer?"]
    declined = installer.install([ORIGIN], port=companion_up, ui=Yes({"Set up local AI on this computer?": False}), program_args=["k.exe"], run=Recorder(), adapter=adapter, health_wait=3, ollama_installer=lambda u: calls.append(2) or (True, "x"))
    assert not declined.ok and calls == [1]

class Opener:
    def __init__(self, data=b"MZ"):
        self.data = data

    def __call__(self, url, timeout=0):
        return io.BytesIO(self.data)


def test_the_windows_runtime_download_is_refused_unless_ollamas_and_validly_signed(tmp_path):
    ui = Yes()
    assert installer.install_ollama_windows(ui, url="https://evil.example/OllamaSetup.exe", workdir=tmp_path)[0] is False
    assert installer.install_ollama_windows(ui, url="http://ollama.com/download/OllamaSetup.exe", workdir=tmp_path)[0] is False
    unsigned = Recorder({"stdout": "REFUSED NotSigned"})
    ok, why = installer.install_ollama_windows(ui, run=unsigned, opener=Opener(), workdir=tmp_path)
    assert ok is False and "signed by Ollama" in why and not any("/VERYSILENT" in " ".join(c) for c in unsigned.calls)  # never run if the signature is not valid
    good = Recorder({"stdout": "VALID"})
    ok, why = installer.install_ollama_windows(ui, run=good, opener=Opener(), workdir=tmp_path)
    assert ok is True and any("/VERYSILENT" in c for call in good.calls for c in call)
    failing = Recorder({"stdout": "VALID", "/VERYSILENT": 3})
    failing.results[str(tmp_path / "OllamaSetup.exe")] = 3
    ok, why = installer.install_ollama_windows(ui, run=lambda cmd, **k: type("R", (), {"returncode": 3 if cmd[0].endswith("OllamaSetup.exe") else 0, "stdout": "VALID", "stderr": ""})(), opener=Opener(), workdir=tmp_path)
    assert ok is False and "ollama.com" in why
    ok, why = installer.install_ollama_windows(ui, run=good, opener=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")), workdir=tmp_path)
    assert ok is False and "internet" in why


def test_windows_uninstall_removes_the_task_and_files_and_cleans_up_a_locked_folder(win, monkeypatch):
    adapter, home = win
    installer.install([ORIGIN], ui=Yes(), program_args=["k.exe"], run=Recorder(), adapter=adapter, health_wait=0.2)
    d = adapter.app_dir()
    d.mkdir(parents=True, exist_ok=True)
    rec, spawned = Recorder(), []
    monkeypatch.setattr(P.WindowsAdapter, "remove_files", lambda self, p: spawned.append(p))  # a running program cannot delete its own folder: the adapter defers it
    res = installer.uninstall(run=rec, adapter=adapter, app_dir=d)
    assert res.ok and [c[1] for c in rec.calls] == ["/End", "/Delete"] and not adapter.service_path("ai.knovent.companion").exists() and spawned == [d]


def test_an_onedir_folder_is_installed_whole_and_one_already_in_place_is_left_alone(tmp_path, monkeypatch):
    src = tmp_path / "dl" / "knovent-companion"
    (src / "_internal").mkdir(parents=True)
    (src / "knovent-companion.exe").write_text("exe")
    (src / "_internal" / "python311.dll").write_text("dll")
    monkeypatch.setattr(installer.sys, "executable", str(src / "knovent-companion.exe"), raising=False)
    monkeypatch.setattr(installer.sys, "frozen", True, raising=False)
    app = tmp_path / "Local" / "Knovent"
    app.mkdir(parents=True)
    dest = installer._install_program(app, installer._run)
    assert dest == app / "app" / "knovent-companion.exe" and (app / "app" / "_internal" / "python311.dll").exists()
    monkeypatch.setattr(installer.sys, "executable", str(dest), raising=False)
    assert installer._install_program(app, installer._run) == dest  # run from its install folder (the Inno setup put it there): nothing is copied over itself


def test_windows_spawn_flags_hide_the_console_and_detach_the_runtime(tmp_path):
    kw = P.WindowsAdapter(env={}, home=tmp_path).spawn_kwargs()
    assert "start_new_session" not in kw and kw["creationflags"] & 0x00000008 and kw["creationflags"] & 0x08000000
    assert P.LinuxAdapter(env={}, home=tmp_path).spawn_kwargs() == {"start_new_session": True}


def test_the_installer_chooses_windows_dialogs_and_falls_back_to_the_console(monkeypatch):
    from faqengine.companion import uiselect

    monkeypatch.setitem(__import__("sys").modules, "faqengine.companion.tkui", None)  # tkinter unavailable
    assert type(uiselect.select_ui("windows")).__name__ == "ConsoleUI"
    assert type(uiselect.select_ui("linux")).__name__ == "ConsoleUI"
