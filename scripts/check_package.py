"""Check an exported package in isolation. Run it with a Python that has ONLY numpy, onnxruntime and tokenizers:

    python scripts/check_package.py dist/my-business.zip examples/my-business/test.csv

It unzips the package, starts its HTTP server, exercises the API (including bad input and the API key), and
re-measures accuracy on the test questions so the number can be compared with the evaluation report.
"""
from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

PORT = 8089


def call(path: str, body=None, key: str | None = None, raw: bytes | None = None, port: int = PORT):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST" if data is not None else "GET")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def serve(pkg: Path, port: int, env_key: str | None = None) -> subprocess.Popen:
    env = {"PATH": "/usr/bin:/bin"}
    if env_key:
        env["FAQENGINE_API_KEY"] = env_key
    proc = subprocess.Popen([sys.executable, str(pkg / "runtime" / "serve.py"), "--package", str(pkg), "--port", str(port)],
                            cwd=pkg, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(100):
        try:
            call("/health", port=port)
            return proc
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.2)
    proc.kill()
    raise SystemExit("server did not start: " + proc.stderr.read().decode()[-500:])


def main(zip_path: str, test_csv: str) -> None:
    tmp = Path(tempfile.mkdtemp())
    zipfile.ZipFile(zip_path).extractall(tmp)
    pkg = next(p for p in tmp.iterdir() if p.is_dir())
    mods = sorted(m for m in sys.modules if m.split(".")[0] in ("torch", "transformers", "faqengine"))
    print(f"unzipped to {pkg}; torch/transformers/faqengine loaded: {mods or 'none'}")

    sys.path.insert(0, str(pkg / "runtime"))
    from engine import Engine  # the package's own copy

    engine = Engine(pkg)
    rows = list(csv.DictReader(open(test_csv, encoding="utf-8")))
    ids = [e["id"] for e in engine.entries]
    hits1 = mrr = n = 0
    for row in rows:
        gold = (row.get("entry_id") or "").strip()
        if gold in ("", "-", "oos"):
            continue
        order = [h["entry_id"] for h in engine.retrieve(row["query"], k=len(ids))]
        rank = order.index(gold) + 1
        n += 1
        hits1 += rank == 1
        mrr += 1 / rank if rank <= 10 else 0
    print(f"standalone engine on {n} answerable test questions: Hit@1 {hits1 / n:.3f}, MRR@10 {mrr / n:.3f}")

    proc = serve(pkg, PORT)
    try:
        print("health:", call("/health"))
        for q in ("can I try a bike out before buying?", "what time do you shut on saturdays", "who won the football last night"):
            code, r = call("/answer", {"query": q})
            print(f"  {code} {q!r} -> {r['decision']} (confidence {r['confidence']:.2f}, {r['latency_ms']:.0f} ms) top: {r['hits'][0]['entry_id']}")
        code, r = call("/retrieve", {"query": "do you deliver", "k": 3})
        print("  retrieve:", code, [(h["entry_id"], round(h["score"], 2)) for h in r["hits"]])
        checks = {
            "empty query": (call("/answer", {"query": "  "}), 400),
            "k out of range": (call("/retrieve", {"query": "x", "k": 99}), 400),
            "not JSON": (call("/answer", raw=b"not json"), 400),
            "unknown path": (call("/nope"), 404),
            "body too large": (call("/answer", raw=b'{"query": "' + b"a" * 70000 + b'"}'), 413),
        }
        for name, ((code, _), want) in checks.items():
            print(f"  {name}: {code} {'ok' if code == want else 'EXPECTED ' + str(want)}")
            assert code == want
    finally:
        proc.terminate()
    secured = serve(pkg, PORT + 1, env_key="s3cret")
    try:
        results = {"no key": call("/answer", {"query": "hi"}, port=PORT + 1)[0], "wrong key": call("/answer", {"query": "hi"}, key="nope", port=PORT + 1)[0],
                   "right key": call("/answer", {"query": "hi"}, key="s3cret", port=PORT + 1)[0], "health stays open": call("/health", port=PORT + 1)[0]}
        print("api key:", results)
        assert results == {"no key": 401, "wrong key": 401, "right key": 200, "health stays open": 200}
    finally:
        secured.terminate()
    print("package check passed")


if __name__ == "__main__":
    main(*sys.argv[1:3])
