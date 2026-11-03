"""Which pieces of knowledge are current. A source can say when it stops being valid (`valid_until`), when it starts (`valid_from`, also written `effective`), and which earlier source it
replaces (`supersedes`). Material that has expired, has not started, or has been replaced must never be treated as current: it is left out of retrieval, and when a question is only
answered by such material the response says so instead of answering. Standard library only (the exported runtime uses it).

Dates are ISO (2026-12-31). Anything else is ignored with a note, never guessed.
"""
from __future__ import annotations

import re
from datetime import date

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value) -> date | None:
    text = str(value or "").strip()
    if not ISO.match(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def base(name: str) -> str:
    """A source named in a `supersedes` line, in a comparable form: the file name (or the web address) in lower case, any folder dropped."""
    return str(name or "").strip().lower().rstrip("/").rsplit("/", 1)[-1]


def inactive_reasons(items: list[dict], as_of: str | date | None = None) -> dict[str, str]:
    """{item id: why it is not current}. Each item has `id`, `source`, and optionally `valid_from`, `valid_until`, `supersedes` (a source name, or several separated by commas).
    A replacing source only counts once it is itself in force on `as_of`, so a policy announced for next month does not retire this month's."""
    today = parse_date(as_of) if not isinstance(as_of, date) else as_of
    today = today or date.today()
    out: dict[str, str] = {}
    in_force: dict[str, bool] = {}
    for it in items:
        start, end = parse_date(it.get("valid_from")), parse_date(it.get("valid_until"))
        src = it.get("source") or ""
        ok = not ((start and today < start) or (end and today > end))
        in_force[src] = in_force.get(src, True) and ok
        if start and today < start:
            out[it["id"]] = f"not valid before {start.isoformat()}"
        elif end and today > end:
            out[it["id"]] = f"expired on {end.isoformat()}"
    replaced: dict[str, str] = {}
    for it in items:
        if it.get("supersedes") and in_force.get(it.get("source") or "", True):
            for old in re.split(r"[,;|]", str(it["supersedes"])):
                if base(old):
                    replaced[base(old)] = it.get("source") or ""
    for it in items:
        why = replaced.get(base(it.get("source") or ""))
        if why and base(it.get("source") or "") != base(why) and it["id"] not in out:
            out[it["id"]] = f"superseded by {why}"
    return out
