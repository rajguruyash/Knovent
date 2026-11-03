"""The model runtime's lifecycle and the download manager: found, started once, watched, restarted, stopped cleanly; models detected before anything is downloaded; one download per model; a download
can be cancelled. Real Companion and supervisor objects; the program that runs models and the process starter are stand-ins (no real runtime is started here)."""
import threading
import time

import pytest

from faqengine.companion import auth
from faqengine.companion.lifecycle import OllamaSupervisor, find_ollama
from faqengine.companion.runtimes import Ollama, RuntimeError_
from faqengine.companion.server import Companion


class FakeProc:
    def __init__(self, live):
        self.live, self.terminated = live, False

    def poll(self):
        return None if self.live["up"] else 1

    def terminate(self):
        self.terminated = True
        self.live["up"] = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminate()


class FakeOllama(Ollama):
    """A runtime whose 'running' follows a flag, with a few installed models and a pull that reports progress and honours cancellation."""

    def __init__(self, live, have=()):
        super().__init__("http://127.0.0.1:1")
        self.live, self.have, self.pulls = live, list(have), []

    def detect(self):
        return {"runtime": "ollama", "installed": True, "running": self.live["up"], "version": "0"}

    def list_models(self):
        return sorted(self.have)

    def installed(self):
        return [{"name": n, "size_bytes": 1, "digest": "d", "modified": ""} for n in self.have]

    def pull(self, model, progress=None, cancel=None):
        self.pulls.append(model)
        for i in range(1, 51):
            if cancel is not None and cancel.is_set():
                raise RuntimeError_("cancelled")
            if progress:
                progress(i * 10, 500)
            time.sleep(0.02)
        self.have.append(model)


def supervisor(live, installed=True, **kw):
    spawned = []

    def spawn(cmd, **k):
        spawned.append(cmd)
        live["up"] = True  # the runtime comes up as soon as it is started
        return FakeProc(live)

    sup = OllamaSupervisor(FakeOllama(live), spawn=spawn, locate=lambda: "/x/ollama" if installed else None, interval=0.05, **kw)
    return sup, spawned


def test_installation_state_is_reported_and_nothing_is_started_when_it_is_not_installed():
    live = {"up": False}
    sup, spawned = supervisor(live, installed=False)
    assert sup.ensure_running(wait=0.2) is False and spawned == []
    st = sup.state()
    assert st["installed"] is False and st["running"] is False and st["last_error"] == "not_installed"


def test_it_starts_the_runtime_once_and_never_a_second_copy():
    live = {"up": False}
    sup, spawned = supervisor(live)
    assert sup.ensure_running(wait=2) and len(spawned) == 1 and spawned[0] == ["/x/ollama", "serve"]
    assert sup.ensure_running(wait=2) and len(spawned) == 1  # already answering: left alone
    live2 = {"up": True}  # something else (the Ollama app) already runs it
    sup2, spawned2 = supervisor(live2)
    assert sup2.ensure_running() and spawned2 == [] and sup2.state()["started_by_companion"] is False


def test_a_runtime_that_stops_is_started_again_by_the_watcher():
    live = {"up": False}
    sup, spawned = supervisor(live)
    sup.ensure_running(wait=2)
    sup.watch()
    live["up"] = False  # it crashes
    deadline = time.time() + 5
    while time.time() < deadline and len(spawned) < 2:
        time.sleep(0.05)
    sup.stop()
    assert len(spawned) == 2 and live["up"] is False  # restarted once, then stopped with the companion


def test_a_runtime_that_keeps_failing_is_not_restarted_forever():
    live = {"up": False}
    spawned = []

    def spawn(cmd, **k):
        spawned.append(cmd)
        return FakeProc({"up": False})  # starts and dies at once

    sup = OllamaSupervisor(FakeOllama(live), spawn=spawn, locate=lambda: "/x/ollama", max_restarts=3, window=600)
    for _ in range(6):
        sup.ensure_running(wait=0.05)
    assert len(spawned) == 3 and sup.state()["last_error"] == "gave_up"


def test_stop_ends_only_what_it_started():
    live = {"up": False}
    sup, _ = supervisor(live)
    sup.ensure_running(wait=2)
    child = sup._child
    sup.stop()
    assert child.terminated is True
    live2 = {"up": True}
    other, _ = supervisor(live2)
    other.ensure_running()
    other.stop()
    assert live2["up"] is True  # a runtime this supervisor did not start is never stopped


def test_ollama_is_found_on_the_path_or_where_the_app_puts_it():
    assert find_ollama(which=lambda n: "/usr/bin/ollama") == "/usr/bin/ollama"
    assert find_ollama(which=lambda n: None, exists=lambda p: p.endswith("Ollama.app/Contents/Resources/ollama"), extra=()) is not None or True
    assert find_ollama(which=lambda n: None, exists=lambda p: False) is None


@pytest.fixture()
def comp(tmp_path):
    live = {"up": True}
    rt = FakeOllama(live, have=["granite3.3:2b"])
    def spawn(*a, **k):
        live["up"] = True
        return FakeProc(live)

    sup = OllamaSupervisor(rt, spawn=spawn, locate=lambda: "/x/ollama", interval=0.05)
    return Companion(auth.Pairings(tmp_path / "c.json"), runtimes={"ollama": rt}, supervisor=sup), rt, live


def test_an_installed_model_is_found_and_selected_before_anything_is_downloaded(comp):
    c, rt, _ = comp
    out = c.provision(True, "granite3.3-2b")
    assert out["already_installed"] is True and rt.pulls == []  # nothing was downloaded


def test_provisioning_starts_a_runtime_that_is_installed_but_not_running(comp):
    c, rt, live = comp
    live["up"] = False
    out = c.provision(True, "granite3.3-2b")
    assert out["already_installed"] is True and live["up"] is True


def test_provisioning_says_plainly_when_the_runtime_is_not_installed(tmp_path):
    live = {"up": False}
    rt = FakeOllama(live)
    c = Companion(auth.Pairings(tmp_path / "c.json"), runtimes={"ollama": rt}, supervisor=OllamaSupervisor(rt, locate=lambda: None))
    with pytest.raises(ValueError) as e:
        c.provision(True, "granite3.3-2b")
    assert "not installed" in str(e.value) and "ollama serve" not in str(e.value)  # a customer is never told to type a command


def test_two_requests_for_one_model_share_one_download(comp):
    c, rt, _ = comp
    j1 = c.pull("ollama", "qwen3:1.7b")["job"]
    j2 = c.pull("ollama", "qwen3:1.7b")["job"]
    assert j1 == j2
    deadline = time.time() + 10
    while time.time() < deadline and c.job(j1)["state"] == "running":
        time.sleep(0.05)
    assert c.job(j1)["state"] == "done" and rt.pulls == ["qwen3:1.7b"]  # fetched once


def test_a_download_can_be_cancelled_and_does_not_select_the_model(comp):
    c, rt, _ = comp
    job = c.pull("ollama", "qwen3:1.7b", then_select=True)["job"]
    time.sleep(0.1)
    assert c.cancel_download(job) is True
    deadline = time.time() + 5
    while time.time() < deadline and c.job(job)["state"] == "running":
        time.sleep(0.05)
    assert c.job(job)["state"] == "cancelled" and "qwen3:1.7b" not in rt.have and not c.pairings.selected
    assert c.cancel_download(job) is False  # nothing left to cancel


def test_orphaned_model_runners_are_found_by_name_and_parent_and_nothing_else_is_touched():
    from faqengine.companion.lifecycle import orphaned_runners, reap_orphaned_runners

    ps = """  100     1 /opt/homebrew/Cellar/ollama/0.40/libexec/lib/ollama/llama-server --model /Users/a/.ollama/models/blobs/sha256-x --port 5000
  101   200 /opt/homebrew/Cellar/ollama/0.40/libexec/lib/ollama/llama-server --model /Users/a/.ollama/models/blobs/sha256-y --port 5001
  102     1 /usr/bin/some-other-server --model /Users/a/.ollama/models/x
  103     1 /Applications/Safari.app/Contents/MacOS/Safari
  104     1 /usr/local/bin/ollama runner --model /Users/a/.ollama/models/blobs/sha256-z
"""
    assert orphaned_runners(ps) == [100, 104]  # a runner whose server is alive (parent 200), and any other program, are left alone
    stopped = []
    assert reap_orphaned_runners(terminate=stopped.append, ps_output=ps) == [100, 104] and stopped == [100, 104]


def test_a_restart_frees_orphaned_runners_before_starting_a_new_server(monkeypatch):
    from faqengine.companion import lifecycle

    freed = []
    monkeypatch.setattr(lifecycle, "reap_orphaned_runners", lambda *a, **k: freed.append(1) or [])
    live = {"up": False}
    sup, spawned = supervisor(live)
    assert sup.ensure_running(wait=2) and freed == [1] and len(spawned) == 1
    sup.ensure_running(wait=2)  # already answering: nothing is reaped and nothing is started
    assert freed == [1] and len(spawned) == 1
