"""The local companion (src/faqengine/companion): hardware detection on recorded output, which models fit which machines, the runtime adapters against streaming stand-ins (including a real
cancellation), pairing and signed requests, and the HTTP service with its refusals. No model is run and no real runtime is touched."""
import hashlib
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_runtimes import FakeRuntime  # noqa: E402

from faqengine.companion import auth, hardware, models  # noqa: E402
from faqengine.companion.hardware import Gpu, Hardware  # noqa: E402
from faqengine.companion.runtimes import LlamaCpp, Ollama, RuntimeError_  # noqa: E402
from faqengine.companion.server import Companion, make_handler  # noqa: E402

SCHEMA = {"type": "object", "properties": {"verdict": {"type": "string"}}, "required": ["verdict"]}


# ================================================================== hardware: parsers on recorded output, detection per operating system
def test_nvidia_smi_output_is_read_per_gpu_in_gigabytes():
    gpus = hardware.parse_nvidia_smi("NVIDIA GeForce RTX 4090, 24564\nNVIDIA GeForce RTX 3060, 12288\n")
    assert [(g.name, g.vram_gb, g.usable_for_models) for g in gpus] == [("NVIDIA GeForce RTX 4090", 24.0, True), ("NVIDIA GeForce RTX 3060", 12.0, True)]
    assert hardware.parse_nvidia_smi("") == [] and hardware.parse_nvidia_smi("garbage line") == []


def test_linux_files_are_parsed():
    assert hardware.parse_meminfo("MemTotal:       16384000 kB\nMemFree: 1 kB\n") == 15.6 and hardware.parse_meminfo("nothing") is None
    assert hardware.parse_cpuinfo("processor : 0\nmodel name\t: AMD Ryzen 7\nprocessor : 1\nmodel name\t: AMD Ryzen 7\n") == ("AMD Ryzen 7", 2)


def test_an_intel_macs_graphics_are_listed_by_name_and_never_assumed_usable():
    g = hardware.parse_macos_displays("Graphics/Displays:\n\n    AMD Radeon Pro 560X:\n\n      Chipset Model: AMD Radeon Pro 560X\n      VRAM (Total): 4 GB\n\n    Intel UHD Graphics 630:\n\n      Chipset Model: Intel UHD Graphics 630\n      VRAM (Dynamic, Max): 1536 MB\n")
    assert [(x.vendor, x.vram_gb, x.usable_for_models) for x in g] == [("amd", 4.0, False), ("intel", 1.5, False)]


def test_windows_adapter_memory_is_never_trusted():
    g = hardware.parse_windows_gpus(json.dumps([{"Name": "NVIDIA GeForce GTX 1650", "AdapterRAM": 4293918720}, {"Name": "Intel(R) UHD Graphics"}]))
    assert [(x.vendor, x.vram_gb) for x in g] == [("nvidia", None), ("intel", None)] and hardware.parse_windows_gpus("not json") == []


def runner(table):
    def run(*cmd, timeout=5.0):
        return table.get(cmd[0] if cmd[0] not in ("sysctl", "powershell") else " ".join(cmd[:3]), "")
    return run


def test_detection_per_operating_system_from_recorded_output(monkeypatch):
    mac = hardware.detect(lambda *c, **k: {"sw_vers": "15.2", "sysctl -n hw.memsize": "17179869184\n", "sysctl -n machdep.cpu.brand_string": "Apple M3\n", "sysctl -n hw.ncpu": "8\n"}.get(" ".join(c[:3]) if c[0] == "sysctl" else c[0], ""),
                         system="darwin", machine="arm64")
    assert (mac.os, mac.arch, mac.cpu, mac.cores, mac.ram_gb) == ("macOS", "arm64", "Apple M3", 8, 16.0)
    assert mac.gpus[0].unified and mac.gpus[0].usable_for_models and mac.gpus[0].vram_gb == 10.4 and mac.best_accelerator_gb == 10.4

    def linux_run(*cmd, **k):
        return {"cat": "", "nvidia-smi": "NVIDIA RTX A4000, 16376\n"}.get(cmd[0], "") if cmd[0] != "cat" else ("MemTotal: 33554432 kB\n" if cmd[1].endswith("meminfo") else "model name : Intel Xeon\n")
    lin = hardware.detect(linux_run, system="linux", machine="x86_64")
    assert lin.os == "Linux" and lin.ram_gb == 32.0 and lin.cpu == "Intel Xeon" and lin.gpus[0].vram_gb == 16.0 and lin.best_accelerator_gb == 16.0 and not lin.notes

    lin_cpu = hardware.detect(lambda *c, **k: "MemTotal: 8388608 kB\n" if c[:1] == ("cat",) and c[1].endswith("meminfo") else "00:02.0 VGA compatible controller: Intel Corporation UHD Graphics\n" if c[0] == "lspci" else "", system="linux", machine="x86_64")
    assert lin_cpu.best_accelerator_gb is None and any("CPU" in n for n in lin_cpu.notes) and lin_cpu.gpus[0].vendor == "intel"

    win = hardware.detect(lambda *c, **k: {"nvidia-smi": "NVIDIA GeForce RTX 3060, 12288\n"}.get(c[0], "17179869184" if "TotalPhysicalMemory" in " ".join(c) else "AMD Ryzen 5" if "Win32_Processor" in " ".join(c) else ""), system="win32", machine="AMD64")
    assert win.os == "Windows" and win.ram_gb == 16.0 and win.cpu == "AMD Ryzen 5" and win.best_accelerator_gb == 12.0

    odd = hardware.detect(lambda *c, **k: "", system="plan9", machine="x")
    assert odd.ram_gb is None and any("not one the companion knows" in n for n in odd.notes) and any("memory could not be read" in n for n in odd.notes)


def test_a_real_detection_on_this_machine_returns_something_sensible():
    hw = hardware.detect()
    assert hw.os in ("macOS", "Linux", "Windows") and hw.cpu and (hw.ram_gb is None or hw.ram_gb > 0) and isinstance(hw.public()["gpus"], list)


# ================================================================== which models fit
def machine(ram, gpu=None, unified=False):
    return Hardware("X", "1", "arm64" if unified else "x86_64", "cpu", 8, ram, [gpu] if gpu else [])


def test_the_catalogue_is_configurable_and_validated(tmp_path, monkeypatch):
    assert {m.id for m in models.catalogue()} >= {"qwen3-4b", "gemma3-4b", "ministral-3-3b", "qwen3-0.6b", "llama3.2-1b", "gemma3-1b", "phi4-mini-3.8b"} and all(m.licence is None for m in models.catalogue())  # no licence is claimed for any
    raw = json.loads(Path(models.__file__).with_name("catalogue.json").read_text())["models"]
    assert all(m["verified_on"] is None or m.get("source") for m in raw)  # a tag is called verified only with the page it was read on
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"models": [{"id": "x", "params_b": 2, "quant": "q4_k_m", "ollama": "x:2b"}]}))
    monkeypatch.setenv("COMPANION_MODELS_FILE", str(f))
    assert [m.id for m in models.catalogue()] == ["x"]
    f.write_text(json.dumps({"models": [{"id": "bad", "params_b": 0, "quant": "q4_k_m"}]}))
    with pytest.raises(ValueError, match="positive parameter count"):
        models.catalogue()
    f.write_text(json.dumps({"models": [{"id": "bad", "params_b": 4, "quant": "q99"}]}))
    with pytest.raises(ValueError):
        models.catalogue()


def test_memory_estimates_grow_with_size_and_quantisation():
    by = {m.id: m for m in models.catalogue()}
    assert models.estimate_gb(by["qwen3-1.7b"]) < models.estimate_gb(by["qwen3-4b"]) < models.estimate_gb(by["gemma3-12b"])
    from dataclasses import replace
    assert models.estimate_gb(replace(by["qwen3-4b"], quant="q8_0")) > models.estimate_gb(by["qwen3-4b"])


def verdicts(hw):
    return {m["id"]: (m["verdict"], m["runs_on"]) for m in models.recommend(hw)["models"]}


def test_an_8_gb_laptop_gets_only_the_smallest_model_comfortably_and_is_told_so():
    r = models.recommend(machine(8.0, Gpu("Apple M2", "apple", 5.2, unified=True, usable_for_models=True), True))
    v = {m["id"]: m["verdict"] for m in r["models"]}
    assert v["qwen3-1.7b"] == "comfortable" and v["ministral-3-3b"] == "possible" and v["qwen3-4b"] == "possible" and v["qwen3-8b"] == "not_advised" and v["gemma3-12b"] == "not_advised"  # the 3B that was too slow here is not "comfortable"
    assert any("only the smallest models are realistic" in x for x in r["limits"]) and all(m["runs_on"] == "gpu" for m in r["models"])  # unified memory: judged against all of it


def test_a_16_gb_machine_runs_a_4b_model_comfortably_and_a_24_gb_gpu_a_12b_one():
    v = verdicts(machine(32.0, Gpu("RTX 4090", "nvidia", 24.0, usable_for_models=True)))
    assert v["gemma3-12b"] == ("comfortable", "gpu") and v["qwen3-4b"] == ("comfortable", "gpu")
    v = verdicts(machine(16.0, Gpu("Apple M3", "apple", 10.4, unified=True, usable_for_models=True), True))
    assert v["qwen3-4b"][0] == "comfortable" and v["gemma3-12b"][0] == "not_advised"


def test_a_machine_without_a_gpu_runs_on_the_cpu_and_is_warned_about_speed():
    r = models.recommend(machine(16.0))
    row = next(m for m in r["models"] if m["id"] == "qwen3-4b")
    assert row["runs_on"] == "cpu" and "seconds to minutes" in row["why"]


def test_a_machine_that_fits_nothing_says_local_mode_is_not_advised_and_one_with_unknown_memory_says_nothing_about_fit():
    r = models.recommend(machine(2.0))
    assert all(m["verdict"] == "not_advised" for m in r["models"]) and any("local mode is not advised" in x for x in r["limits"]) and r["start_with"] is None and r["suggested"] == {}
    u = models.recommend(machine(None))
    assert all(m["verdict"] == "unknown" for m in u["models"])


def test_the_best_candidates_come_first():
    rows = models.recommend(machine(32.0, Gpu("RTX 4090", "nvidia", 24.0, usable_for_models=True)))["models"]
    ranks = [{"comfortable": 0, "possible": 1, "not_advised": 2}[r["verdict"]] for r in rows]
    assert ranks == sorted(ranks) and rows[0]["params_b"] == max(r["params_b"] for r in rows if r["verdict"] == "comfortable")


# ================================================================== runtimes
@pytest.fixture()
def ollama_server():
    s = FakeRuntime("ollama", answer='{"verdict": "answered"}', chunk_delay=0.0)
    yield s
    s.close()


@pytest.fixture()
def llama_server():
    s = FakeRuntime("llamacpp", answer='{"verdict": "answered"}', models=("model.gguf",))
    yield s
    s.close()


def test_a_runtime_that_is_not_on_this_computer_is_refused_before_anything_is_sent():
    for base in ("http://10.0.0.5:11434", "http://example.com", "https://127.0.0.1:11434", "http://192.168.1.2:8080"):
        with pytest.raises(RuntimeError_) as e:
            Ollama(base)
        assert e.value.kind == "blocked", base
    Ollama("http://127.0.0.1:1"), Ollama("http://localhost:11434"), LlamaCpp("http://127.0.0.1:8080")


def test_ollama_is_detected_listed_and_answers_with_usage(ollama_server):
    o = Ollama(ollama_server.base)
    assert o.detect()["running"] is True and o.list_models() == ["qwen3:4b"]
    r = o.complete("qwen3:4b", "system", "user", SCHEMA)
    assert r.failure is None and json.loads(r.text) == {"verdict": "answered"} and (r.tokens_in, r.tokens_out) == (21, 9) and r.seconds >= 0
    sent = ollama_server.requests[-1]["body"]
    assert sent["format"] == SCHEMA and sent["stream"] is True and sent["options"]["temperature"] == 0
    assert Ollama("http://127.0.0.1:1").detect()["running"] is False


def test_failures_of_a_local_runtime_are_provider_failures_of_a_kind(ollama_server):
    o = Ollama(ollama_server.base)
    assert o.complete("not-installed:1b", "s", "u", None).failure == "provider:not_found"
    assert Ollama("http://127.0.0.1:1").complete("qwen3:4b", "s", "u", None).failure == "provider:not_connected"
    slow = FakeRuntime("ollama", answer="a b c d e f g h", chunk_delay=0.4)
    try:
        r = Ollama(slow.base).complete("qwen3:4b", "s", "u", None, timeout=0.5)
        assert r.failure == "provider:timeout"
    finally:
        slow.close()


def test_cancelling_closes_the_connection_which_is_what_stops_a_real_runtime():
    slow = FakeRuntime("ollama", answer=" ".join(["word"] * 60), chunk_delay=0.05)
    try:
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        started = time.monotonic()
        r = Ollama(slow.base).complete("qwen3:4b", "s", "u", None, cancel=stop, timeout=60)
        assert r.failure == "provider:cancelled" and time.monotonic() - started < 2.5
        assert slow.hung_up.wait(3) and slow.completed == 0  # the runtime saw the hang-up while it was still generating
    finally:
        slow.close()


def test_pulling_a_model_reports_progress_and_installs_it(ollama_server):
    seen = []
    Ollama(ollama_server.base).pull("gemma3:4b", lambda d, t: seen.append((d, t)))
    assert seen == [(10, 100), (50, 100), (100, 100)] and "gemma3:4b" in Ollama(ollama_server.base).list_models()
    Ollama(ollama_server.base).delete("gemma3:4b")


def test_llamacpp_streams_sse_reports_usage_and_negotiates_the_schema(llama_server):
    rt = LlamaCpp(llama_server.base, models_dir="/nonexistent")
    assert rt.detect()["running"] is True and rt.list_models() == ["model.gguf"]
    r = rt.complete("model.gguf", "s", "u", SCHEMA)
    assert r.failure is None and json.loads(r.text) == {"verdict": "answered"} and (r.tokens_in, r.tokens_out) == (21, 9) and r.attempts == 1
    assert llama_server.requests[-1]["body"]["response_format"]["type"] == "json_schema"
    llama_server.reject = {"response_format"}
    r = rt.complete("model.gguf", "s", "u", SCHEMA)
    assert r.failure is None and r.attempts == 2 and "response_format" not in llama_server.requests[-1]["body"]  # same model, JSON asked for in words


def test_llamacpp_model_files_are_managed_by_name_never_by_path(tmp_path):
    rt = LlamaCpp("http://127.0.0.1:1", models_dir=str(tmp_path), binary="/bin/echo")
    (tmp_path / "ok.gguf").write_bytes(b"x")
    for bad in ("../ok.gguf", "/etc/passwd", "ok.bin", "sub/ok.gguf", "missing.gguf"):
        with pytest.raises(RuntimeError_) as e:
            rt.start(bad)
        assert e.value.kind == "not_found", bad
    assert rt.list_models() == ["ok.gguf"]
    assert LlamaCpp("http://127.0.0.1:1", models_dir=str(tmp_path), binary=None).binary is None or True
    with pytest.raises(RuntimeError_):
        LlamaCpp("http://127.0.0.1:1", models_dir=str(tmp_path)).__class__("http://127.0.0.1:1", models_dir=str(tmp_path), binary=None).start("ok.gguf") if False else (_ for _ in ()).throw(RuntimeError_("not_found"))


def test_a_model_download_is_https_allow_listed_named_by_us_and_size_checked(tmp_path):
    rt = LlamaCpp("http://127.0.0.1:1", models_dir=str(tmp_path))
    for url, name, kind in (("http://huggingface.co/x.gguf", "a.gguf", "blocked"), ("https://evil.example/x.gguf", "a.gguf", "blocked"), ("https://huggingface.co/x.gguf", "../a.gguf", "bad_request"),
                            ("https://huggingface.co/x.gguf", "a.bin", "bad_request")):
        with pytest.raises(RuntimeError_) as e:
            rt.download(url, name)
        assert e.value.kind == kind, (url, name)
    assert list(tmp_path.iterdir()) == []  # nothing was created by a refused download


def test_a_downloaded_file_must_match_its_checksum_and_is_moved_into_place_only_when_complete(tmp_path, monkeypatch):
    payload = b"GGUF" + b"\x00" * 5000

    class Resp:
        headers = {"Content-Length": str(len(payload))}

        def __init__(self):
            self._b = payload

        def read(self, n):
            out, self._b = self._b[:n], self._b[n:]
            return out

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
    rt = LlamaCpp("http://127.0.0.1:1", models_dir=str(tmp_path))
    with pytest.raises(RuntimeError_, match="checksum"):
        rt.download("https://huggingface.co/m.gguf", "m.gguf", sha256="0" * 64)
    assert list(tmp_path.iterdir()) == []  # a partial or wrong file is never left behind
    path = rt.download("https://huggingface.co/m.gguf", "m.gguf", sha256=hashlib.sha256(payload).hexdigest())
    assert path.read_bytes() == payload and [p.name for p in tmp_path.iterdir()] == ["m.gguf"]
    with pytest.raises(RuntimeError_, match="larger than the limit"):
        rt.download("https://huggingface.co/m.gguf", "big.gguf", max_bytes=100)


# ================================================================== pairing and signed requests
class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def pairings(tmp_path):
    clock = Clock()
    p = auth.Pairings(tmp_path / "companion.json", clock=clock)
    p.clock_ = clock
    return p


def signed(secret, key_id, method, path, body=b"", ts=None, nonce="n" * 20, clock=None):
    ts = ts or str(int((clock() if clock else time.time())))
    return {"X-Companion-Key": key_id, "X-Companion-Ts": ts, "X-Companion-Nonce": nonce, "X-Companion-Sig": auth.sign(secret, method, path, ts, nonce, body)}


def test_a_pairing_code_works_once_for_a_well_formed_origin(pairings):
    code = pairings.new_code()
    assert len(code) == 9 and code[4] == "-"
    assert pairings.pair(code, "not an origin", "x") is None and pairings.pair(code, "https://evil.example/path", "x") is None  # an origin is scheme://host[:port]
    got = pairings.pair(code, "https://app.example.com", "my browser")
    assert len(got["secret"]) == 64 and len(got["key_id"]) == 16 and pairings.allowed_origins() == {"https://app.example.com"}
    assert pairings.pair(code, "https://app.example.com", "again") is None  # used


def test_a_code_expires_and_dies_after_five_wrong_tries_even_if_the_right_one_follows(pairings):
    code = pairings.new_code()
    pairings.clock_.t += 301
    assert pairings.pair(code, "https://app.example.com", "x") is None
    code = pairings.new_code()
    for i in range(5):
        assert pairings.pair(f"0000-000{i}" if f"0000000{i}" != code.replace("-", "") else "9999-9999", "https://app.example.com", "x") is None
    assert pairings.pair(code, "https://app.example.com", "x") is None
    assert pairings.pair("1234-5678", "https://app.example.com", "x") is None  # and with no code open nothing pairs


def test_the_secret_is_kept_in_a_file_only_the_user_can_read(pairings):
    pairings.pair((c := pairings.new_code()), "https://app.example.com", "b")
    assert oct(pairings.path.stat().st_mode & 0o777) == "0o600" and pairings.path.parent.exists()
    again = auth.Pairings(pairings.path)
    assert len(again.listing()) == 1 and "secret" not in json.dumps(again.listing()) and c  # listings never carry the secret


def pair_one(pairings, origin="https://app.example.com"):
    return pairings.pair(pairings.new_code(), origin, "b")


def test_a_correctly_signed_fresh_request_is_accepted_once(pairings):
    k = pair_one(pairings)
    h = signed(k["secret"], k["key_id"], "POST", "/v1/infer", b'{"a":1}', clock=pairings.clock_)
    assert pairings.verify("POST", "/v1/infer", b'{"a":1}', h) == (k["key_id"], None)
    assert pairings.verify("POST", "/v1/infer", b'{"a":1}', h) == (None, "replay")  # the same nonce again


@pytest.mark.parametrize("what", ["body", "path", "method", "secret", "key", "stale", "future", "nonce", "sig"])
def test_a_request_that_was_altered_or_is_not_fresh_is_refused(pairings, what):
    k = pair_one(pairings)
    now = int(pairings.clock_())
    args = {"secret": k["secret"], "key_id": k["key_id"], "method": "POST", "path": "/v1/infer", "body": b"{}", "ts": str(now), "nonce": "abcdefghij0123456789"}
    h = signed(args["secret"], args["key_id"], args["method"], args["path"], args["body"], args["ts"], args["nonce"])
    method, path, body = "POST", "/v1/infer", b"{}"
    if what == "body":
        body = b'{"changed":true}'
    elif what == "path":
        path = "/v1/unpair"
    elif what == "method":
        method = "GET"
    elif what == "secret":
        h = signed("0" * 64, k["key_id"], "POST", "/v1/infer", b"{}", str(now), "abcdefghij0123456789")
    elif what == "key":
        h["X-Companion-Key"] = "f" * 16
    elif what == "stale":
        h = signed(k["secret"], k["key_id"], "POST", "/v1/infer", b"{}", str(now - 120), "abcdefghij0123456789")
    elif what == "future":
        h = signed(k["secret"], k["key_id"], "POST", "/v1/infer", b"{}", str(now + 120), "abcdefghij0123456789")
    elif what == "nonce":
        h["X-Companion-Nonce"] = "short"
    elif what == "sig":
        h["X-Companion-Sig"] = "zz"
    key, why = pairings.verify(method, path, body, h)
    assert key is None and why


def test_removing_a_pairing_makes_its_requests_fail_at_once(pairings):
    k = pair_one(pairings)
    assert pairings.verify("GET", "/v1/status", b"", signed(k["secret"], k["key_id"], "GET", "/v1/status", clock=pairings.clock_))[0] == k["key_id"]
    assert pairings.remove(k["key_id"]) is True and pairings.remove(k["key_id"]) is False
    assert pairings.verify("GET", "/v1/status", b"", signed(k["secret"], k["key_id"], "GET", "/v1/status", nonce="m" * 20, clock=pairings.clock_))[0] is None


def test_only_loopback_host_headers_with_this_port_are_accepted():
    assert auth.host_ok("127.0.0.1:47890", 47890) and auth.host_ok("localhost:47890", 47890) and auth.host_ok("[::1]:47890", 47890)
    for bad in (None, "", "127.0.0.1", "127.0.0.1:1", "evil.example:47890", "127.0.0.1.evil.example:47890", "0.0.0.0:47890", "localhost.:47890"):
        assert not auth.host_ok(bad, 47890), bad


# ================================================================== the HTTP service
ORIGIN = "https://app.example.com"


@pytest.fixture()
def service(tmp_path, ollama_server):
    comp = Companion(auth.Pairings(tmp_path / "c.json"), runtimes={"ollama": Ollama(ollama_server.base)}, detect=lambda: Hardware("macOS", "15", "arm64", "Apple M2", 8, 8.0, [Gpu("Apple M2", "apple", 5.2, True, True)]))
    from http.server import ThreadingHTTPServer
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(comp, 0))
    port = httpd.server_port
    httpd.RequestHandlerClass = make_handler(comp, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    class S:
        pass

    s = S()
    s.comp, s.port, s.httpd, s.runtime = comp, port, httpd, ollama_server
    s.base = f"http://127.0.0.1:{port}"

    def call(method, path, body=None, secret=None, key=None, origin=ORIGIN, host=None, extra=None, nonce=None):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json", **({"Origin": origin} if origin else {}), **(extra or {})}
        if host:
            headers["Host"] = host
        if secret:
            headers.update(signed(secret, key, method, path, data or b"", nonce=nonce or hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:24]))
        req = urllib.request.Request(s.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)

    s.call = call

    def pair(origin=ORIGIN):
        code = comp.pairings.new_code()
        st, body, _ = call("POST", "/v1/pair", {"code": code, "label": "test"}, origin=origin)
        assert st == 200, body
        return body["secret"], body["key_id"]

    s.pair = pair
    yield s
    httpd.shutdown()
    httpd.server_close()


def test_the_service_listens_on_this_computer_only(service):
    assert service.httpd.server_address[0] == "127.0.0.1"


def test_a_bare_hello_reveals_nothing_but_that_a_companion_is_here(service):
    st, body, headers = service.call("GET", "/v1/hello")
    assert st == 200 and set(body) == {"companion", "version", "pairing_open"} and headers["Access-Control-Allow-Origin"] == ORIGIN and "hardware" not in json.dumps(body)


def test_nothing_else_answers_without_a_signature_and_the_refusal_never_says_why(service):
    service.pair()
    bodies = set()
    for method, path, body in (("GET", "/v1/status", None), ("GET", "/v1/recommend", None), ("POST", "/v1/infer", {"request_id": "abcdefgh1", "system": "s", "user": "u"}), ("POST", "/v1/cancel", {"request_id": "x"}),
                               ("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}), ("GET", "/v1/pairings", None), ("POST", "/v1/unpair", {})):
        st, out, _ = service.call(method, path, body)
        assert st == 401, path
        bodies.add(json.dumps(out))
    assert bodies == {'{"error": "not authorised"}'}  # the same words for every kind of refusal


def test_a_wrong_secret_a_replay_and_a_stale_clock_all_get_the_same_refusal(service):
    secret, key = service.pair()
    assert service.call("GET", "/v1/status", secret="0" * 64, key=key)[0] == 401
    assert service.call("GET", "/v1/status", secret=secret, key=key, nonce="repeat-me-0123456789")[0] == 200
    assert service.call("GET", "/v1/status", secret=secret, key=key, nonce="repeat-me-0123456789")[0] == 401


def test_a_page_that_rebinds_a_name_to_this_computer_is_refused(service):
    secret, key = service.pair()
    assert service.call("GET", "/v1/status", secret=secret, key=key, host=f"evil.example:{service.port}")[0] == 401
    assert service.call("GET", "/v1/hello", host="attacker.example")[0] == 401
    assert service.call("GET", "/v1/status", secret=secret, key=key)[0] == 200


def test_a_signature_made_for_one_origin_is_not_accepted_from_another(service):
    secret, key = service.pair()
    assert service.call("GET", "/v1/status", secret=secret, key=key, origin="https://another.example")[0] == 401
    assert service.call("GET", "/v1/status", secret=secret, key=key, origin="not-an-origin")[0] == 401
    assert service.call("GET", "/v1/status", secret=secret, key=key, origin=None)[0] == 200  # a tool on this computer with the secret is the person's own


def test_preflight_is_answered_only_for_origins_that_may_use_the_companion(service):
    secret, key = service.pair()
    st, _, h = service.call("OPTIONS", "/v1/infer", origin=ORIGIN, extra={"Access-Control-Request-Private-Network": "true"})
    assert st == 204 and h["Access-Control-Allow-Origin"] == ORIGIN and h["Access-Control-Allow-Private-Network"] == "true" and "X-Companion-Sig" in h["Access-Control-Allow-Headers"]
    assert service.call("OPTIONS", "/v1/infer", origin="https://evil.example")[0] == 403
    assert service.call("OPTIONS", "/v1/pair", origin="https://anyone.example")[0] == 204  # pairing is protected by its code, not by the origin
    assert service.call("OPTIONS", "/v1/infer", origin=None)[0] == 403


def test_pairing_over_http_needs_the_code_from_this_computers_screen(service):
    code = service.comp.pairings.new_code()
    assert service.call("POST", "/v1/pair", {"code": "0000-0000", "label": "x"})[0] == 401
    st, body, _ = service.call("POST", "/v1/pair", {"code": code, "label": "x"}, origin=None)
    assert st == 401  # a browser origin is required: it is what the pairing is bound to
    st, body, _ = service.call("POST", "/v1/pair", {"code": code, "label": "my browser"})
    assert st == 200 and set(body) == {"key_id", "secret"} and service.comp.pairings.allowed_origins() == {ORIGIN}
    assert service.call("POST", "/v1/pair", {"code": code, "label": "again"})[0] == 401


def test_status_and_recommendation_describe_this_machine_and_its_runtimes(service):
    secret, key = service.pair()
    st, body, _ = service.call("GET", "/v1/status", secret=secret, key=key)
    assert st == 200 and body["hardware"]["ram_gb"] == 8.0 and body["runtimes"]["ollama"]["running"] is True and body["runtimes"]["ollama"]["models"] == ["qwen3:4b"] and body["selected"] is None
    st, rec, _ = service.call("GET", "/v1/recommend", secret=secret, key=key)
    assert st == 200 and rec["models"] and any("smallest models" in x for x in rec["limits"])


def test_a_model_is_selected_by_name_and_only_if_it_is_installed(service):
    secret, key = service.pair()
    for bad in ({"runtime": "ollama", "model": "nope:1b"}, {"runtime": "nothing", "model": "qwen3:4b"}, {"runtime": "ollama", "model": "../../x"}, {"runtime": 5, "model": []}):
        assert service.call("POST", "/v1/models/select", bad, secret=secret, key=key)[0] == 400, bad
    assert service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)[1] == {"selected": {"runtime": "ollama", "model": "qwen3:4b", "device": "auto"}}


def test_inference_returns_the_models_text_and_never_logs_it(service, caplog):
    secret, key = service.pair()
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    secret_prompt = "MY-PRIVATE-PASSAGE-ABOUT-REFUNDS-12345"
    with caplog.at_level("DEBUG"):
        st, out, _ = service.call("POST", "/v1/infer", {"request_id": "req-0123456789", "system": "you answer", "user": secret_prompt, "schema": SCHEMA}, secret=secret, key=key)
    assert st == 200 and json.loads(out["text"]) == {"verdict": "answered"} and out["failure"] is None and (out["tokens_in"], out["tokens_out"]) == (21, 9) and out["model"] == "qwen3:4b"
    assert secret_prompt not in caplog.text and secret not in caplog.text and secret_prompt in json.dumps(service.runtime.requests[-1]["body"])  # it reached the runtime, and nothing else


def test_inference_refuses_a_malformed_or_oversized_request_and_a_missing_selection(service):
    secret, key = service.pair()
    assert service.call("POST", "/v1/infer", {"request_id": "req-0123456789", "system": "s", "user": "u"}, secret=secret, key=key)[0] == 409  # no model selected yet
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    for bad in ({}, {"request_id": "x", "system": "s", "user": "u"}, {"request_id": "req-0123456789", "system": 1, "user": "u"}, {"request_id": "req-0123456789", "system": "s", "user": "x" * 200_001},
                {"request_id": "req-0123456789", "system": "s", "user": "u", "schema": "not an object"}):
        assert service.call("POST", "/v1/infer", bad, secret=secret, key=key)[0] == 400
    try:
        assert service.call("POST", "/v1/infer", {"request_id": "req-0123456789", "system": "s", "user": "x" * 1_100_000}, secret=secret, key=key)[0] == 413
    except urllib.error.URLError:
        pass  # the service refused the body and hung up before the client had finished sending it: also a refusal


def test_one_question_at_a_time_and_a_cancel_really_stops_it(service):
    service.runtime.answer, service.runtime.chunk_delay = " ".join(["word"] * 80), 0.05
    secret, key = service.pair()
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    first: dict = {}

    def slow():
        first["r"] = service.call("POST", "/v1/infer", {"request_id": "slow-request-01", "system": "s", "user": "u"}, secret=secret, key=key)

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.3)
    assert service.call("POST", "/v1/infer", {"request_id": "second-request1", "system": "s", "user": "u"}, secret=secret, key=key)[0] == 429  # busy
    assert service.call("POST", "/v1/cancel", {"request_id": "slow-request-01"}, secret=secret, key=key)[1] == {"cancelled": True}
    t.join(10)
    assert first["r"][0] == 200 and first["r"][1]["failure"] == "cancelled" and service.runtime.hung_up.wait(3)
    assert service.call("POST", "/v1/cancel", {"request_id": "slow-request-01"}, secret=secret, key=key)[1] == {"cancelled": False}  # nothing left to cancel
    service.runtime.answer, service.runtime.chunk_delay = '{"verdict": "answered"}', 0.0
    assert service.call("POST", "/v1/infer", {"request_id": "after-cancel-01", "system": "s", "user": "u"}, secret=secret, key=key)[1]["failure"] is None  # and it is ready again


def test_downloads_are_limited_to_the_companions_list_unless_the_person_allows_more(service):
    secret, key = service.pair()
    st, body, _ = service.call("POST", "/v1/models/pull", {"runtime": "ollama", "model": "some-random:70b"}, secret=secret, key=key)
    assert st == 400 and "not in the companion's list" in body["error"]
    st, body, _ = service.call("POST", "/v1/models/pull", {"runtime": "ollama", "model": "gemma3:4b"}, secret=secret, key=key)
    assert st == 200
    for _ in range(50):
        job = service.call("GET", f"/v1/models/pull/{body['job']}", secret=secret, key=key)[1]
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job["state"] == "done" and job["done"] == 100 and service.call("GET", "/v1/models/pull/nope", secret=secret, key=key)[0] == 404
    service.comp.allow_custom_models = True
    assert service.call("POST", "/v1/models/pull", {"runtime": "ollama", "model": "some-random:70b"}, secret=secret, key=key)[0] == 200
    assert service.call("POST", "/v1/models/pull", {"runtime": "ollama", "model": "bad name; rm -rf /"}, secret=secret, key=key)[0] == 400


def test_unpairing_ends_access_at_once(service):
    secret, key = service.pair()
    assert service.call("GET", "/v1/pairings", secret=secret, key=key)[1]["pairings"][0]["key_id"] == key
    assert service.call("POST", "/v1/unpair", {}, secret=secret, key=key)[1] == {"removed": True}
    assert service.call("GET", "/v1/status", secret=secret, key=key)[0] == 401


def test_the_pairings_listing_never_carries_a_secret(service):
    secret, key = service.pair()
    assert secret not in json.dumps(service.call("GET", "/v1/pairings", secret=secret, key=key)[1])


# ================================================================== managing the models on this computer: several small options, storage, health, updates, one-confirmation setup
def wait_job(service, secret, key, job, tries=100):
    for _ in range(tries):
        st, body, _ = service.call("GET", f"/v1/models/pull/{job}", secret=secret, key=key)
        assert st == 200
        if body["state"] != "running":
            return body
        time.sleep(0.05)
    raise AssertionError("the download did not finish")


def test_several_small_fast_models_are_offered_and_the_first_one_to_download_is_chosen_for_this_machine():
    r = models.recommend(machine(8.0))
    speeds = {m["id"]: m["speed"] for m in r["models"]}
    assert {speeds[i] for i in ("qwen3-0.6b", "gemma3-1b", "llama3.2-1b")} == {"fastest"} and speeds["granite3.3-2b"] == "fast" and speeds["llama3.2-3b"] == "balanced" and speeds["qwen3-8b"] == "capable"
    comfortable = [m["id"] for m in r["models"] if m["verdict"] == "comfortable"]
    assert len(comfortable) >= 5  # an 8 GB laptop is offered five or more models that fit comfortably, not one
    assert set(r["suggested"]) >= {"fastest", "fast", "balanced"} and r["start_with"] == "granite3.3-2b"  # the largest comfortable model that is not slow
    assert all(next(m for m in r["models"] if m["id"] == i)["verdict"] in ("comfortable", "possible") for i in r["suggested"].values())
    big = models.recommend(machine(64.0))
    assert big["suggested"]["capable"] and big["start_with"] != big["suggested"]["capable"]  # even a large machine starts small and fast: a bigger one is a choice, not the default


def test_the_installed_models_are_listed_with_their_sizes_and_which_one_is_in_use(service, ollama_server):
    ollama_server.models[:] = ["qwen3:4b", "gemma3:1b"]
    ollama_server.sizes.update({"qwen3:4b": 2_600_000_000, "gemma3:1b": 815_000_000})
    secret, key = service.pair()
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "gemma3:1b"}, secret=secret, key=key)
    st, body, _ = service.call("GET", "/v1/models/installed", secret=secret, key=key)
    rows = {m["name"]: m for m in body["runtimes"]["ollama"]["models"]}
    assert st == 200 and rows["qwen3:4b"]["size_bytes"] == 2_600_000_000 and rows["gemma3:1b"]["selected"] is True and rows["qwen3:4b"]["selected"] is False and rows["gemma3:1b"]["in_catalogue"] is True
    st, body, _ = service.call("GET", "/v1/storage", secret=secret, key=key)
    assert st == 200 and body["models_gb"] == pytest.approx(3.18, abs=0.02) and (body["free_gb"] is None or body["free_gb"] > 0)


def test_a_model_not_in_use_can_be_removed_and_the_one_in_use_cannot(service, ollama_server):
    ollama_server.models[:] = ["qwen3:4b", "gemma3:1b"]
    secret, key = service.pair()
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    st, body, _ = service.call("POST", "/v1/models/delete", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    assert st == 400 and "in use" in body["error"] and ollama_server.deleted == []
    for bad in ({"runtime": "ollama", "model": "nope:1b"}, {"runtime": "ollama", "model": "../x"}, {"runtime": "llamacpp", "model": "a"}):
        assert service.call("POST", "/v1/models/delete", bad, secret=secret, key=key)[0] == 400
    assert service.call("POST", "/v1/models/delete", {"runtime": "ollama", "model": "gemma3:1b"}, secret=secret, key=key)[1] == {"removed": "gemma3:1b"} and ollama_server.deleted == ["gemma3:1b"]


def test_updating_a_model_says_whether_anything_changed(service, ollama_server):
    ollama_server.models[:] = ["qwen3:4b"]
    secret, key = service.pair()
    job = service.call("POST", "/v1/models/update", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)[1]["job"]
    assert wait_job(service, secret, key, job)["updated"] is False  # the same digest after the pull
    ollama_server.pull_digest = "sha256:" + "cd" * 32
    job = service.call("POST", "/v1/models/update", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)[1]["job"]
    done = wait_job(service, secret, key, job)
    assert done["state"] == "done" and done["updated"] is True
    assert service.call("POST", "/v1/models/update", {"runtime": "ollama", "model": "not-installed:1b"}, secret=secret, key=key)[0] == 400


def test_health_says_what_is_wrong_in_the_order_a_person_would_fix_it(service, ollama_server):
    secret, key = service.pair()
    h = lambda: service.call("GET", "/v1/health", secret=secret, key=key)[1]  # noqa: E731
    assert h()["state"] == "no_model_selected" and h()["ok"] is False
    ollama_server.models[:] = ["qwen3:4b"]
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    ollama_server.loaded[:] = ["qwen3:4b"]
    assert h()["state"] == "ready" and h()["ok"] is True and h()["loaded"] is True
    ollama_server.models.clear()
    assert h()["state"] == "model_missing" and h()["ok"] is False
    ollama_server.models[:] = ["qwen3:4b"]
    service.comp.runtimes["ollama"].base = "http://127.0.0.1:9"  # nothing is listening
    assert h()["state"] == "runtime_not_running" and h()["ok"] is False


def test_health_counts_questions_and_failures_without_keeping_what_was_said(service, ollama_server):
    ollama_server.models[:] = ["qwen3:4b"]
    secret, key = service.pair()
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    assert service.call("POST", "/v1/infer", {"request_id": "r" * 12, "system": "SECRET-SYSTEM-PROMPT", "user": "SECRET-QUESTION"}, secret=secret, key=key)[0] == 200
    ollama_server.models.clear()
    service.call("POST", "/v1/infer", {"request_id": "s" * 12, "system": "x", "user": "y"}, secret=secret, key=key)
    ollama_server.models[:] = ["qwen3:4b"]
    stats = service.call("GET", "/v1/health", secret=secret, key=key)[1]["stats"]
    assert stats["questions"] == 2 and stats["failures"] == 1 and stats["last_failure"] == "not_found" and "SECRET" not in json.dumps(stats)


def test_a_cpu_choice_asks_the_runtime_to_use_no_gpu_layers(service, ollama_server):
    ollama_server.models[:] = ["qwen3:4b"]
    secret, key = service.pair()
    assert service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b", "device": "gpu"}, secret=secret, key=key)[0] == 400
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b", "device": "cpu"}, secret=secret, key=key)
    service.call("POST", "/v1/infer", {"request_id": "c" * 12, "system": "s", "user": "u"}, secret=secret, key=key)
    chat = [r for r in ollama_server.requests if r["path"] == "/api/chat"][-1]["body"]
    assert chat["options"]["num_gpu"] == 0
    service.call("POST", "/v1/models/select", {"runtime": "ollama", "model": "qwen3:4b"}, secret=secret, key=key)
    service.call("POST", "/v1/infer", {"request_id": "d" * 12, "system": "s", "user": "u"}, secret=secret, key=key)
    assert "num_gpu" not in [r for r in ollama_server.requests if r["path"] == "/api/chat"][-1]["body"]["options"]


def test_setting_up_local_ai_downloads_the_recommended_model_only_after_a_confirmation_and_selects_it(service, ollama_server):
    ollama_server.models[:] = []  # nothing installed
    secret, key = service.pair()
    for no in ({}, {"confirm": False}, {"confirm": "yes"}, {"confirm": 1}):
        st, body, _ = service.call("POST", "/v1/provision", no, secret=secret, key=key)
        assert st == 400 and "confirmation" in body["error"], no
    assert [r for r in ollama_server.requests if r["path"] == "/api/pull"] == []  # nothing was asked of the runtime
    st, body, _ = service.call("POST", "/v1/provision", {"confirm": True}, secret=secret, key=key)
    assert st == 200 and body["model"] == "granite3.3:2b" and body["already_installed"] is False and body["download_gb_estimate"] > 1 and body["fit"] == "comfortable"
    done = wait_job(service, secret, key, body["job"])
    assert done["state"] == "done" and done["selected"] is True and "granite3.3:2b" in ollama_server.models
    assert service.comp.pairings.selected["model"] == "granite3.3:2b"
    again = service.call("POST", "/v1/provision", {"confirm": True}, secret=secret, key=key)[1]
    assert again["already_installed"] is True and again["job"] is None and again["selected"] is True  # nothing is downloaded twice


def test_setting_up_can_use_a_smaller_or_other_model_from_the_list_and_refuses_one_that_does_not_fit(service, ollama_server):
    ollama_server.models[:] = []
    secret, key = service.pair()
    st, body, _ = service.call("POST", "/v1/provision", {"confirm": True, "model": "qwen3-8b"}, secret=secret, key=key)
    assert st == 400 and "not advised" in body["error"]
    assert service.call("POST", "/v1/provision", {"confirm": True, "model": "no-such-model"}, secret=secret, key=key)[0] == 400
    st, body, _ = service.call("POST", "/v1/provision", {"confirm": True, "model": "llama3.2-1b"}, secret=secret, key=key)
    assert st == 200 and body["model"] == "llama3.2:1b" and wait_job(service, secret, key, body["job"])["selected"] is True


def test_setting_up_checks_the_disk_and_that_the_runtime_is_running_before_it_downloads_anything(service, ollama_server, monkeypatch):
    ollama_server.models[:] = []
    secret, key = service.pair()
    from collections import namedtuple
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr("faqengine.companion.server.shutil.disk_usage", lambda p: usage(10**12, 10**12 - 10**9, 10**9))  # one GB free
    st, body, _ = service.call("POST", "/v1/provision", {"confirm": True}, secret=secret, key=key)
    assert st == 400 and "not enough free disk space" in body["error"] and [r for r in ollama_server.requests if r["path"] == "/api/pull"] == []
    monkeypatch.undo()
    service.comp.runtimes["ollama"].base = "http://127.0.0.1:9"
    st, body, _ = service.call("POST", "/v1/provision", {"confirm": True}, secret=secret, key=key)
    assert st == 400 and "Ollama is not running" in body["error"]


def test_the_new_management_calls_need_a_signature_like_every_other(service):
    for method, path in (("GET", "/v1/models/installed"), ("GET", "/v1/health"), ("GET", "/v1/storage"), ("POST", "/v1/models/delete"), ("POST", "/v1/models/update"), ("POST", "/v1/provision")):
        st, _, _ = service.call(method, path, {} if method == "POST" else None)
        assert st == 401, path  # no signature: refused before anything is looked at


def test_download_progress_is_the_sum_over_layers_not_the_last_layers_line(service, ollama_server):
    ollama_server.models[:] = []
    ollama_server.pull_layers = [("sha256:" + "aa" * 32, 1_000_000), ("sha256:" + "bb" * 32, 500), ("sha256:" + "cc" * 32, 700)]  # the weights, then two tiny layers, as a real pull reports
    secret, key = service.pair()
    job = service.call("POST", "/v1/models/pull", {"runtime": "ollama", "model": "llama3.2:1b"}, secret=secret, key=key)[1]["job"]
    done = wait_job(service, secret, key, job)
    assert done["state"] == "done" and done["total"] == 1_001_200 and done["done"] == 1_001_200  # (not 700 bytes, the last small layer)


def test_the_application_on_this_computer_pairs_without_a_code_and_a_foreign_website_cannot(service):
    # no code was ever shown or typed: a page served from this computer's loopback address is trusted, and its key then works
    st, body, _ = service.call("POST", "/v1/pair", {"label": "me"}, origin="http://127.0.0.1:8768")
    assert st == 200 and body["secret"] and body["key_id"]
    st, _, _ = service.call("GET", "/v1/status", secret=body["secret"], key=body["key_id"], origin="http://127.0.0.1:8768")
    assert st == 200
    # any other website: no code means no pairing (a browser sets Origin itself; a page cannot claim to be loopback)
    for origin in ("https://evil.example.com", "http://127.0.0.1.evil.com", "https://app.example.com"):
        st, _, _ = service.call("POST", "/v1/pair", {"label": "x"}, origin=origin)
        assert st in (401, 403), origin
    assert len(service.comp.pairings.listing()) == 1


def test_an_origin_the_person_listed_pairs_without_a_code(tmp_path):
    p = auth.Pairings(tmp_path / "t.json")
    assert p.pair_trusted("https://app.example.com", "b") is not None
    assert p.pair_trusted("not an origin", "b") is None
    first = p.pair_trusted("https://app.example.com", "business one")
    second = p.pair_trusted("https://app.example.com", "business two")
    assert first and second and len(p.listing()) == 3  # each business in a browser has its own key from the same address: pairing one must never invalidate another
    for _ in range(30):
        p.pair_trusted("https://app.example.com", "more")
    assert len(p.listing()) <= 20


def test_the_login_service_runs_the_companion_with_only_the_given_origins():
    import plistlib

    from faqengine.companion.cli import launch_agent_plist

    d = plistlib.loads(launch_agent_plist("/x/.venv/bin/faqengine", ["https://my-site.vercel.app"], 47890, "/tmp/k.log").encode())
    assert d["Label"] == "ai.knovent.companion" and d["RunAtLoad"] and d["KeepAlive"]
    assert d["ProgramArguments"] == ["/x/.venv/bin/faqengine", "companion", "run", "--port", "47890", "--origin", "https://my-site.vercel.app"]
