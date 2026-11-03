"""WHICH WEBSITES MAY PAIR WITHOUT A CODE: one versioned file, not a start-up argument.

    {"schema": 1, "production": ["https://app.example.com"], "development": ["http://knovent.localhost:8768"], "updated": "..."}

* **production** (written by the installer from the build's configuration): exact `https` hostnames only. No wildcard, no path, no user name, no IP address, no `localhost`.
* **development** (written only by an explicit developer action, never by a production build): additionally `http://localhost`, `http://127.0.0.1` and `*.localhost` with a port.
* The companion re-reads the file when it changes, so a new site address takes effect without restarting the companion or the computer. A damaged or unknown-version file trusts nothing.
* Origins given on a command line (`--origin`, `FAQENGINE_APP_ORIGIN`) are session-only developer additions, validated as development origins and never written to the file.
A browser sets `Origin` itself and a page cannot forge it; trusting an address means "pages from exactly this address may pair with this companion", nothing more."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .auth import default_config_path

SCHEMA = 1
HOST = r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
PORT = r"(?::\d{1,5})?"
PRODUCTION = re.compile(rf"^https://{HOST}{PORT}$")
DEVELOPMENT = re.compile(rf"^(?:https://{HOST}{PORT}|http://(?:localhost|127\.0\.0\.1|(?:[a-z0-9-]+\.)+localhost){PORT})$")


def validate_origin(origin: object, development: bool = False) -> str:
    """The origin in canonical form (lower case, no trailing slash), or ValueError saying why it is not acceptable."""
    if not isinstance(origin, str):
        raise ValueError("an origin must be text")
    o = origin.strip().rstrip("/").lower()
    if any(c in o for c in "*@ ?#") or o.count("://") != 1:
        raise ValueError(f"{origin!r}: no wildcards, user names, paths or spaces")
    if not (DEVELOPMENT if development else PRODUCTION).match(o):
        raise ValueError(f"{origin!r}: a production origin must be an exact https host name" if not development else f"{origin!r}: not an https host or a localhost address")
    return o


def default_path() -> Path:
    return default_config_path().parent / "trusted_origins.json"


class TrustedOrigins:
    def __init__(self, path: Path | None = None, session: tuple[str, ...] | list[str] = (), check_every: float = 2.0, clock=time.monotonic):
        self.path, self.check_every, self.clock = path or default_path(), check_every, clock
        self._lock = threading.Lock()
        self._sig: tuple | None = None
        self._checked = -1e9
        self.production: set[str] = set()
        self.development: set[str] = set()
        self.session: set[str] = set()
        self.warnings: list[str] = []
        for o in session:
            self.session.add(validate_origin(o, development=True))  # a command line origin is a developer's choice: valid as a development origin, never saved
        self._read()

    # ---- reading (also whenever the file changes)
    def _read(self) -> None:
        try:
            st = self.path.stat()
            sig = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        with self._lock:
            if sig == self._sig and self._sig is not None:
                return
            self._sig, self.warnings = sig, []
            prod: set[str] = set()
            dev: set[str] = set()
            if sig is not None:
                try:
                    d = json.loads(self.path.read_text())
                    if not isinstance(d, dict) or d.get("schema") != SCHEMA:
                        raise ValueError("unknown schema")
                    for key, target, is_dev in (("production", prod, False), ("development", dev, True)):
                        for o in d.get(key, []):
                            try:
                                target.add(validate_origin(o, development=is_dev))
                            except ValueError as e:
                                self.warnings.append(str(e))
                except (OSError, ValueError, TypeError) as e:
                    self.warnings.append(f"trusted_origins.json ignored: {e}")
                    prod, dev = set(), set()  # a damaged file trusts nothing
            self.production, self.development = prod, dev

    def _refresh(self) -> None:
        now = self.clock()
        if now - self._checked >= self.check_every:
            self._checked = now
            self._read()

    def __contains__(self, origin: object) -> bool:
        self._refresh()
        return isinstance(origin, str) and (origin in self.production or origin in self.development or origin in self.session)

    def __bool__(self) -> bool:
        self._refresh()
        return bool(self.production or self.development or self.session)

    def all(self) -> list[str]:
        self._refresh()
        return sorted(self.production | self.development | self.session)

    # ---- writing (the installer, or a developer's explicit action)
    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"schema": SCHEMA, "production": sorted(self.production), "development": sorted(self.development), "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f, indent=2)
        os.replace(tmp, self.path)
        self._sig, self._checked = None, -1e9

    def set_production(self, origins: list[str]) -> list[str]:
        """Replaces the production list (a new build's addresses). Every origin is validated first: one bad entry changes nothing."""
        clean = {validate_origin(o) for o in origins}
        with self._lock:
            self.production = clean
            self._save()
        self._read()
        return sorted(clean)

    def set_development(self, origins: list[str]) -> list[str]:
        clean = {validate_origin(o, development=True) for o in origins}
        with self._lock:
            self.development = clean
            self._save()
        self._read()
        return sorted(clean)
