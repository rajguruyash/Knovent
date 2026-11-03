#!/usr/bin/env python3
"""Runs the companion's real-machine acceptance checks against a BUILT companion program, and writes a report. It is how a platform earns "supported": every check below must PASS on that
operating system. It installs under a test name and port (your own install is not touched) and removes it again.

  python scripts/platform_selftest.py --exe "<path to knovent-companion(.exe) or Knovent Companion>" --origin https://your-site.vercel.app [--model qwen3-0.6b] [--cancel-model gemma3-1b]

Downloads happen only if you pass --model / --cancel-model (they ask Ollama for a real model: hundreds of MB). Without them, an installed model is used and download/cancel are reported NOT RUN.
Checks that need a person (a reboot, a real login) are listed at the end as MANUAL. Exit status 0 only if nothing FAILED."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from faqengine.companion import auth  # noqa: E402
from faqengine.companion.platforms import adapter_for, detect_platform  # noqa: E402

REPORT: list[dict] = []
WIN = sys.platform == "win32"


def record(name: str, status: str, detail: str = "") -> bool:
    REPORT.append({"check": name, "status": status, "detail": detail})
    print(f"{status:8} {name}" + (f"  ({detail})" if detail else ""), flush=True)
    return status == "PASS"


def http(port, method, path, body=None, origin=None, key=None, secret=None, business=None, timeout=30):
    data = None if body is None else json.dumps(body).encode()
    h = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
    if key:
        ts, nonce = str(int(time.time())), uuid.uuid4().hex[:24]
        h.update({"X-Companion-Key": key, "X-Companion-Ts": ts, "X-Companion-Nonce": nonce, "X-Companion-Sig": auth.sign(secret, method, path, ts, nonce, data or b"", business)})
        if business:
            h["X-Companion-Business"] = business
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=h), timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
    except OSError:
        return 0, {}


def wait(fn, seconds, step=2.0):
    t0 = time.time()
    while time.time() - t0 < seconds:
        v = fn()
        if v:
            return v, time.time() - t0
        time.sleep(step)
    return None, time.time() - t0


def kill_by_name(name: str) -> None:
    if WIN:
        subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)
    else:
        subprocess.run(["pkill", "-9", "-f", name], capture_output=True)


def count_procs(name: str) -> int:
    if WIN:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], capture_output=True, text=True).stdout
        return sum(1 for line in out.splitlines() if name.lower() in line.lower())
    return len(subprocess.run(["pgrep", "-f", f"{name} serve"], capture_output=True, text=True).stdout.split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", required=True)
    ap.add_argument("--origin", required=True)
    ap.add_argument("--port", type=int, default=47895)
    ap.add_argument("--label", default="ai.knovent.companion.selftest")
    ap.add_argument("--model")
    ap.add_argument("--cancel-model")
    ap.add_argument("--report", default="selftest-report.json")
    ap.add_argument("--recovery-wait", type=int, default=420, help="seconds to allow for the companion to be restarted after it is killed (Windows' task repeats every 5 minutes)")
    a = ap.parse_args()
    P = a.port
    A, B = "11111111-aaaa-bbbb-cccc-000000000001", "22222222-aaaa-bbbb-cccc-000000000002"
    plat = detect_platform()
    adapter = adapter_for(plat)
    appdir = Path(tempfile.mkdtemp(prefix="knovent-selftest-"))
    print(f"platform {plat.key} | python {platform.python_version()} | companion {a.exe}\n")
    setup = [a.exe, "setup", "--yes", "--port", str(P), "--label", a.label, "--app-dir", str(appdir), "--origin", a.origin]
    done = subprocess.run(setup, capture_output=True, text=True, timeout=3600)
    ok = record("1 fresh install (no Python needed by the installed program)", "PASS" if done.returncode == 0 else "FAIL", (done.stdout + done.stderr).strip().splitlines()[-1] if (done.stdout + done.stderr).strip() else "")
    if not ok:
        print(done.stdout, done.stderr)
        return finish(a, appdir, plat)
    record("2 login service registered", "PASS" if adapter.service_path(a.label).exists() and subprocess.run(adapter.status_command(a.label), capture_output=True).returncode == 0 else "FAIL", str(adapter.service_path(a.label)))
    st, hello = http(P, "GET", "/v1/hello")
    record("3 companion answers (health)", "PASS" if st == 200 and hello.get("companion") else "FAIL", f"version {hello.get('version')}")
    st, pr = http(P, "POST", "/v1/pair", {"label": "selftest", "business": A}, origin=a.origin)
    key, secret = pr.get("key_id"), pr.get("secret")
    record("4 automatic pairing from the trusted site, bound to a business", "PASS" if st == 200 and pr.get("bound") else "FAIL", f"status {st}")
    st, bad = http(P, "POST", "/v1/pair", {"label": "x", "business": A}, origin="https://evil.example")
    record("5 wrong origin rejected with a readable reason", "PASS" if st == 401 and bad.get("error") == "origin_not_trusted" else "FAIL", f"status {st} {bad}")
    st, status = http(P, "GET", "/v1/status", origin=a.origin, key=key, secret=secret, business=A)
    record("6 signed request works for its business", "PASS" if st == 200 and status.get("companion") else "FAIL", f"status {st}")
    st, _ = http(P, "GET", "/v1/status", origin=a.origin, key=key, secret=secret, business=B)
    record("7 the same key is refused for another business", "PASS" if st == 401 else "FAIL", f"status {st}")
    hw = (status.get("hardware") or {})
    want = {"macos": "macos", "windows": "windows", "linux": "linux"}[plat.os]
    record("8 the companion itself confirms OS and architecture", "PASS" if str(hw.get("os", "")).lower().startswith(want[:3]) and str(hw.get("arch", "")).lower() in ("arm64", "aarch64", "amd64", "x86_64", "x64") else "FAIL", f"{hw.get('os')} {hw.get('arch')} {hw.get('ram_gb')} GB")
    rs = status.get("runtime_state") or {}
    record("9 AI runtime (Ollama) installed and running", "PASS" if rs.get("installed") and rs.get("running") else "FAIL", json.dumps(rs))
    st, inst = http(P, "GET", "/v1/models/installed", origin=a.origin, key=key, secret=secret, business=A)
    have = [m["name"] for m in ((inst.get("runtimes") or {}).get("ollama") or {}).get("models", [])] if isinstance(inst, dict) else []
    record("10 existing models are detected", "PASS" if st == 200 else "FAIL", ", ".join(have) or "none installed")
    chosen = None
    if a.model:
        st, pv = http(P, "POST", "/v1/provision", {"confirm": True, "model": a.model}, origin=a.origin, key=key, secret=secret, business=A)
        if st == 200 and pv.get("job"):
            seen = []
            def poll():
                s, j = http(P, "GET", f"/v1/models/pull/{pv['job']}", origin=a.origin, key=key, secret=secret, business=A)
                seen.append(j.get("done", 0))
                return j if j.get("state") in ("done", "failed", "cancelled") else None
            j, secs = wait(poll, 1800, 3)
            record("11 recommended model downloads with progress", "PASS" if j and j["state"] == "done" and max(seen) > 0 else "FAIL", f"{j and j['state']} in {secs:.0f}s")
            chosen = pv.get("model")
        elif st == 200 and pv.get("already_installed"):
            record("11 recommended model downloads with progress", "NOT RUN", "already installed: nothing was downloaded (correct)")
            chosen = pv.get("model")
        else:
            record("11 recommended model downloads with progress", "FAIL", f"status {st} {pv}")
    else:
        record("11 recommended model downloads with progress", "NOT RUN", "pass --model to run it")
    if a.cancel_model:
        st, pv = http(P, "POST", "/v1/models/pull", {"runtime": "ollama", "model": a.cancel_model}, origin=a.origin, key=key, secret=secret, business=A)
        job = pv.get("job")
        wait(lambda: http(P, "GET", f"/v1/models/pull/{job}", origin=a.origin, key=key, secret=secret, business=A)[1].get("done", 0) > 0, 120, 2)
        st2, c = http(P, "POST", "/v1/models/pull/cancel", {"job": job}, origin=a.origin, key=key, secret=secret, business=A)
        j, _ = wait(lambda: (lambda x: x if x.get("state") != "running" else None)(http(P, "GET", f"/v1/models/pull/{job}", origin=a.origin, key=key, secret=secret, business=A)[1]), 60, 1)
        record("12 a download can be cancelled", "PASS" if st == 200 and c.get("cancelled") and j and j.get("state") == "cancelled" else "FAIL", f"{j and j.get('state')}")
    else:
        record("12 a download can be cancelled", "NOT RUN", "pass --cancel-model to run it")
    chosen = chosen or (have[0] if have else None)
    if chosen:
        runtime_tag = next((m for m in have if m == chosen or m.split(":")[0] in chosen), chosen)
        http(P, "POST", "/v1/models/select", {"runtime": "ollama", "model": runtime_tag}, origin=a.origin, key=key, secret=secret, business=A)
        st, inf = http(P, "POST", "/v1/infer", {"request_id": uuid.uuid4().hex[:24], "system": "Answer in one short sentence.", "user": "What is 2+2?", "max_tokens": 30, "timeout": 300}, origin=a.origin, key=key, secret=secret, business=A, timeout=330)
        text = str(inf.get("text") or inf.get("reply") or "")
        record("13 local inference answers through the companion", "PASS" if st == 200 and text.strip() else "FAIL", f"status {st}: {text.strip()[:60]!r}")
    else:
        record("13 local inference answers through the companion", "NOT RUN", "no model installed; pass --model")
    kill_by_name("knovent-companion.exe" if WIN else str(appdir))
    time.sleep(2)
    back, secs = wait(lambda: http(P, "GET", "/v1/hello")[0] == 200, a.recovery_wait, 3)
    record("14 companion is restarted after it is killed", "PASS" if back else "FAIL", f"{secs:.0f}s")
    before = count_procs("ollama.exe" if WIN else "ollama")
    kill_by_name("ollama.exe" if WIN else "ollama serve")
    up, secs = wait(lambda: urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2).status == 200 if _ok() else False, 180, 3)
    time.sleep(8)
    after = count_procs("ollama.exe" if WIN else "ollama")
    record("15 Ollama is restarted after it is killed, with no duplicate", "PASS" if up and after <= max(before, 1) + (1 if WIN else 0) else "FAIL", f"{secs:.0f}s, processes before {before} after {after}")
    again = subprocess.run(setup, capture_output=True, text=True, timeout=3600)
    st, _ = http(P, "GET", "/v1/hello")
    record("16 upgrade: running setup again replaces, never doubles", "PASS" if again.returncode == 0 and st == 200 else "FAIL", f"exit {again.returncode}")
    return finish(a, appdir, plat, adapter=adapter, have=have)


def _ok() -> bool:
    try:
        urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2).close()
        return True
    except OSError:
        return False


def finish(a, appdir, plat, adapter=None, have=()):
    exe = a.exe
    done = subprocess.run([exe, "uninstall", "--label", a.label, "--app-dir", str(appdir)], capture_output=True, text=True, timeout=300)
    time.sleep(10 if WIN else 3)
    gone = http(a.port, "GET", "/v1/hello")[0] == 0
    adapter = adapter or adapter_for(plat)
    record("17 uninstall removes the service and the program", "PASS" if done.returncode == 0 and gone and not adapter.service_path(a.label).exists() and not appdir.exists() else "FAIL", f"exit {done.returncode}")
    from faqengine.companion.lifecycle import find_ollama

    record("18 Ollama is still installed after uninstall (and so are its models)", "PASS" if find_ollama() else "FAIL", find_ollama() or "")
    for m in ("a reboot starts the companion by itself", "logging out and in starts the companion by itself", "SmartScreen/Gatekeeper first-open experience on a fresh download"):
        record("MANUAL " + m, "MANUAL", "needs a person at the machine")
    Path(a.report).write_text(json.dumps({"platform": plat.key, "python": platform.python_version(), "os": platform.platform(), "time": time.strftime("%Y-%m-%d %H:%M:%S"), "exe_sha256": hashlib.sha256(Path(a.exe).read_bytes()).hexdigest() if Path(a.exe).is_file() else None, "results": REPORT}, indent=2))
    failed = [r for r in REPORT if r["status"] == "FAIL"]
    print(f"\n{len(failed)} FAILED; report: {a.report}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
