#!/usr/bin/env python3
"""A READ-ONLY, API-LEVEL check of a Supabase project as an anonymous visitor: do the engine's tables and functions exist (are the migrations there?), and is the anonymous role refused at the
Data API? It needs only the project's URL and its PUBLISHABLE key (the key a browser holds). It creates no account, writes nothing, applies nothing, and cannot prove the rules INSIDE the database:
for that, scripts/validate_supabase.py needs the staging database's connection string.

    python scripts/probe_supabase_anon.py --url https://<ref>.supabase.co --expect-ref <ref> --key-file /path/to/.env --key-var NAME [--forbidden ref1,ref2]

For every table the migrations create: an anonymous GET must be REFUSED (HTTP 401, SQLSTATE 42501: the table exists and `anon` has no privilege). 404 means the table is not there (the migration is not
applied) or is not exposed; 200 means the anonymous role can read it, which is a failure even when no row comes back (it should hold no privilege at all).
For every callable function: an anonymous call, with dummy arguments of the right names, must be REFUSED the same way. Functions that return a trigger are not callable through the API and are skipped.

The key is read from the file, sent only in the `apikey` header to the project's own host and never printed, logged or written (a test proves it). The script refuses a URL that is not
https://<ref>.supabase.co, a ref that is not the one you said you expect, and any ref listed as forbidden (production, and every other product's project)."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
DUMMY = {"uuid": "00000000-0000-4000-8000-000000000000", "text": "x", "text[]": ["x"], "integer": 0, "int": 0, "bigint": 0, "boolean": False, "date": "2026-01-01", "timestamptz": "2026-01-01T00:00:00Z",
         "jsonb": {}, "extensions.vector(384)": "[" + ",".join(["0"] * 384) + "]"}


class Refused(Exception):
    pass


def tables() -> list[str]:
    return sorted({m.group(1) for f in sorted(MIGRATIONS.glob("*.sql")) for m in re.finditer(r"^create table (?:if not exists )?public\.(\w+)", f.read_text(), re.M | re.I)})


def functions() -> dict[str, dict[str, object]]:
    """name -> {argument name: dummy value} for every function the migrations define that is callable through the API (not trigger functions), from the LAST definition of each."""
    out: dict[str, dict[str, object]] = {}
    for f in sorted(MIGRATIONS.glob("*.sql")):
        text = f.read_text()
        for m in re.finditer(r"create or replace function public\.(\w+)\s*\((.*?)\)\s*returns\s+([\w\. ]+?)(?:\s|$)", text, re.S | re.I):
            name, args, returns = m.group(1), m.group(2), m.group(3).strip().lower()
            if returns == "trigger":
                out.pop(name, None)
                continue
            params: dict[str, object] = {}
            for part in [a.strip() for a in re.split(r",(?![^()]*\))", args) if a.strip()]:
                bits = part.split(None, 1)
                if len(bits) != 2:
                    continue
                pname, ptype = bits[0], re.sub(r"\s+default\s+.*$", "", bits[1], flags=re.I).strip().lower()
                params[pname] = DUMMY.get(ptype, "x")
            out[name] = params
    return out


def ref_of(url: str) -> str | None:
    m = re.fullmatch(r"https://([a-z0-9]{20})\.supabase\.(?:co|com)/?", url.strip())
    return m.group(1) if m else None


def read_key(path: str, var: str) -> str:
    for line in Path(path).read_text().splitlines():
        m = re.match(rf"^\s*(?:export\s+)?{re.escape(var)}\s*=\s*(.*?)\s*$", line)
        if m:
            return m.group(1).strip().strip("'\"")
    raise Refused(f"{var} is not in {path}")


def guard(url: str, expect_ref: str, forbidden: set[str]) -> str:
    ref = ref_of(url)
    if ref is None:
        raise Refused("the address must be https://<project ref>.supabase.co")
    if ref in forbidden:
        raise Refused(f"project {ref} is listed as forbidden")
    if ref != expect_ref:
        raise Refused(f"the address is project {ref}, not the one you said to expect ({expect_ref})")
    return ref


def call(url: str, method: str, path: str, key: str, body=None, timeout: float = 20.0) -> tuple[int, dict]:
    req = urllib.request.Request(url.rstrip("/") + path, data=None if body is None else json.dumps(body).encode(), method=method,
                                 headers={"apikey": key, "Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - the address was checked to be https://<ref>.supabase.co
            raw = r.read(65536)
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(65536), e.code
    except OSError as e:
        raise Refused(f"could not reach the project ({type(e).__name__})") from None
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {"rows": len(data)}


def classify(status: int, data: dict) -> str:
    """refused | missing | readable | rejected | other. 'refused' means the DATABASE said the anonymous role lacks the privilege (SQLSTATE 42501, "permission denied"): the right answer for `anon`.
    A 401 or 403 without that is the gateway turning the request away before the database was asked (a rejected key, a missing secret key): it says nothing about the schema, so it is never counted as
    'refused'."""
    code, message = str(data.get("code", "")), str(data.get("message", "")).lower()
    if code == "42501" or "permission denied" in message:
        return "refused"
    if status == 404 or code in ("PGRST205", "PGRST202", "42P01"):
        return "missing"
    if status in (401, 403):
        return "rejected"
    if status in (200, 204, 206):
        return "readable"
    return "other"


def probe(url: str, key: str) -> list[dict]:
    rows = []
    for t in tables():
        s, d = call(url, "GET", f"/rest/v1/{t}?select=*&limit=1", key)
        rows.append({"kind": "table", "name": t, "http": s, "sqlstate": d.get("code"), "result": classify(s, d)})
    for f, params in functions().items():
        s, d = call(url, "POST", f"/rest/v1/rpc/{f}", key, params)
        rows.append({"kind": "function", "name": f, "http": s, "sqlstate": d.get("code"), "result": classify(s, d)})
    return rows


def summarise(rows: list[dict]) -> dict:
    by = lambda r: [x["name"] for x in rows if x["result"] == r]  # noqa: E731
    return {"checked": len(rows), "refused_as_expected": len(by("refused")), "missing": by("missing"), "anonymous_can_reach": by("readable"), "rejected_before_the_database": by("rejected"), "unexpected": by("other")}


def main(argv=None, env=os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--expect-ref", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--key-var", required=True)
    ap.add_argument("--forbidden", default="")
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    forbidden = {x.strip() for x in (args.forbidden + "," + env.get("FORBIDDEN_PROJECT_REFS", "")).split(",") if x.strip()}
    try:
        ref = guard(args.url, args.expect_ref, forbidden)
        key = read_key(args.key_file, args.key_var)
        rows = probe(args.url, key)
    except Refused as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 3
    s = summarise(rows)
    print(f"API-level check as an anonymous visitor of project {ref} (read-only; not a database validation)")
    print(f"  {s['checked']} tables and functions checked; refused as expected: {s['refused_as_expected']}")
    for label, key_ in (("not found (migration not applied, or not exposed)", "missing"), ("REACHABLE by the anonymous role", "anonymous_can_reach"),
                        ("turned away by the gateway before the database was asked (key rejected?): says nothing about the schema", "rejected_before_the_database"), ("unexpected answer", "unexpected")):
        if s[key_]:
            print(f"  {label}: {', '.join(s[key_])}")
    ok = s["refused_as_expected"] == s["checked"]
    print("  result:", "every table and function exists and refuses the anonymous role" if ok else "NOT clean: see above")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps({"project_ref": ref, "summary": s, "rows": rows}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
