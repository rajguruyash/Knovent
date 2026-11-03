#!/usr/bin/env python3
"""Which of the engine's settings are available, as SET or MISSING, from the process environment and the protected file. Never a value: not even a prefix or a length.

    python scripts/env_status.py [--file ~/.config/faq-engine/.env]

The protected file (default ~/.config/faq-engine/.env) is outside the repository and readable by its owner only; this tool also says when it is not (a file anyone on the machine can read, or one that
sits inside the repository, is reported as a problem). To use the file, start the shell from it:   set -a; source ~/.config/faq-engine/.env; set +a"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = Path.home() / ".config" / "faq-engine" / ".env"
GROUPS = {
    "staging project (the database-level validation)": ["STAGING_SUPABASE_URL", "STAGING_PROJECT_REF", "STAGING_CONFIRM_REF", "STAGING_DATABASE_URL", "FORBIDDEN_PROJECT_REFS"],
    "staging project keys": ["Knovent_Publishable_Key", "Knovent_Servicez_Key"],
    "the engine service": ["DATABASE_URL", "ENGINE_SERVICE_SECRET", "EMBEDDING_MODEL_DIR", "LLM_CREDENTIAL_KEYS", "ENGINE_AI_MODES", "ENGINE_PIPELINE"],
    "the platform's own provider accounts (platform mode)": ["PLATFORM_CLOUDFLARE_ACCOUNT_ID", "PLATFORM_CLOUDFLARE_API_TOKEN", "PLATFORM_OPENROUTER_API_KEY"],
    "the benchmark's accounts (not production's)": ["CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "OPENROUTER_API_KEY"],
}
ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def names_in(path: Path) -> dict[str, bool]:
    """name -> has a non-empty value, from the file. Values are not kept."""
    out: dict[str, bool] = {}
    for line in path.read_text().splitlines():
        m = ASSIGN.match(line)
        if m:
            out[m.group(1)] = bool(m.group(2).strip().strip("'\""))
    return out


def problems(path: Path) -> list[str]:
    out = []
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        out.append(f"the file can be read by other users (mode {oct(mode)}): run chmod 600 on it")
    try:
        path.resolve().relative_to(ROOT)
        out.append("the file is inside the repository: move it out")
    except ValueError:
        pass
    return out


def status(path: Path, env=os.environ) -> tuple[dict[str, str], list[str]]:
    in_file = names_in(path) if path.exists() else {}
    names = [n for g in GROUPS.values() for n in g]
    result = {n: ("SET (environment)" if env.get(n) else "SET (file)" if in_file.get(n) else "MISSING") for n in names}
    return result, (problems(path) if path.exists() else [f"{path} does not exist"])


def main(argv=None, env=os.environ) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(DEFAULT))
    args = ap.parse_args(argv)
    path = Path(args.file).expanduser()
    result, issues = status(path, env)
    for group, names in GROUPS.items():
        print(group)
        for n in names:
            print(f"  {n:<34}{result[n]}")
    for i in issues:
        print(f"PROBLEM: {i}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
