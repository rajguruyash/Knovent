"""WHO MAY TALK TO THE COMPANION. It listens on this computer only, but "only this computer" is not "only me": any web page you open can make your browser send a request to 127.0.0.1. So:

  * nothing answers without a SIGNATURE, except the pairing handshake and a bare "is a companion here?" that reveals no hardware, no model and no pairing;
  * pairing is a short code the person reads off the companion's own screen: single use, valid for five minutes, five wrong tries and it is gone. It yields a long random secret, shown once;
  * every request is signed: HMAC-SHA256 over method, path, timestamp, a nonce and the hash of the body, with that secret. A timestamp more than a minute off is refused, and so is a nonce seen
    before (replay). The secret never travels again; in the browser it is a non-extractable key;
  * a request carrying an Origin must come from an origin the person paired from; a Host header that is not this computer's loopback address is refused (DNS rebinding);
  * pairings can be listed and removed; removing one makes every request signed with it fail at once.
Secrets are kept in a file only the user can read (mode 600), since the companion must be able to verify a signature. No prompt, answer or document is ever written down."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets as stdlib_secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

CLOCK_SKEW = 60.0
NONCE_TTL = 180.0
PAIRING_TTL = 300.0
KEY_TTL = 90 * 86400.0  # a browser's key expires after 90 days; a page served from a trusted address then pairs again by itself
BUSINESS = re.compile(r"^[0-9A-Za-z-]{8,64}$")
MAX_WRONG_CODES = 5
ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")
HOSTS = ("127.0.0.1", "localhost", "[::1]")


def default_config_path() -> Path:
    return Path(os.environ.get("COMPANION_CONFIG") or Path.home() / ".config" / "faq-engine" / "companion.json")


class Pairings:
    """The paired browsers: key id -> {secret, origin, label, created}. Persisted at mode 600."""

    def __init__(self, path: Path | None = None, clock=time.time):
        self.path, self.clock = path or default_config_path(), clock
        self._lock = threading.Lock()
        self._data: dict[str, dict] = {}
        self._code: dict | None = None
        self._nonces: OrderedDict[str, float] = OrderedDict()
        self.selected: dict = {}
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text())
                self._data = saved.get("pairings", {})
                self.selected = saved.get("selected", {})
            except (ValueError, OSError):
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"pairings": self._data, "selected": self.selected}, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    # ---- pairing
    def new_code(self) -> str:
        """A fresh one-use code, replacing any earlier one. Shown by the companion on this computer's screen only."""
        n = stdlib_secrets.randbelow(10**8)
        code = f"{n:08d}"
        with self._lock:
            self._code = {"code": code, "expires": self.clock() + PAIRING_TTL, "wrong": 0}
        return f"{code[:4]}-{code[4:]}"

    def pair(self, code: object, origin: object, label: object, business: object = None) -> dict | None:
        """The key id and secret for a correct code from an acceptable origin, else None. The code dies on use, on expiry and after five wrong tries."""
        if not isinstance(code, str) or not isinstance(origin, str) or not ORIGIN.match(origin) or not isinstance(label, str):
            return None
        digits = re.sub(r"\D", "", code)
        with self._lock:
            c = self._code
            if c is None or self.clock() > c["expires"]:
                self._code = None
                return None
            if not hmac.compare_digest(digits.encode(), c["code"].encode()):
                c["wrong"] += 1
                if c["wrong"] >= MAX_WRONG_CODES:
                    self._code = None
                return None
            self._code = None  # single use
            key_id, secret = stdlib_secrets.token_hex(8), stdlib_secrets.token_hex(32)
            self._data[key_id] = self._record(secret, origin, label.strip(), business)
            self._save()
            return {"key_id": key_id, "secret": secret, "bound": isinstance(business, str)}

    def _record(self, secret: str, origin: str, label: str, business: object) -> dict:
        """A key belongs to ONE business in ONE browser address and expires: a request is accepted only for the business it was made for, and only until `expires`."""
        rec = {"secret": secret, "origin": origin.rstrip("/"), "label": label[:80] or "browser", "created": int(self.clock()), "expires": int(self.clock() + KEY_TTL)}
        if isinstance(business, str) and BUSINESS.match(business):
            rec["business"] = business
        return rec

    def pair_trusted(self, origin: object, label: object, business: object = None) -> dict | None:
        """Pairs WITHOUT a code, for an origin the caller has already decided to trust (the application served on this computer, or an origin the person
        listed with --origin). A browser sets Origin itself and a web page cannot forge it, so another website cannot use this. A browser keeps one key per business, all from the same address, so earlier keys stay valid (the newest twenty are kept)."""
        if not isinstance(origin, str) or not ORIGIN.match(origin):
            return None
        label = label.strip()[:80] if isinstance(label, str) else ""
        with self._lock:
            biz = business if isinstance(business, str) and BUSINESS.match(business) else None
            if biz:  # pairing the same business again from the same address replaces ITS key only; other businesses' keys stay valid
                for k in [k for k, v in self._data.items() if v["origin"] == origin.rstrip("/") and v.get("business") == biz]:
                    del self._data[k]
            mine = sorted((k for k, v in self._data.items() if v["origin"] == origin.rstrip("/")), key=lambda k: self._data[k]["created"])
            for k in mine[:-19]:  # one key per business in the browser, all from the same address: keep the newest twenty, never invalidate the others
                del self._data[k]
            key_id, secret = stdlib_secrets.token_hex(8), stdlib_secrets.token_hex(32)
            self._data[key_id] = self._record(secret, origin, label, biz)
            self._save()
            return {"key_id": key_id, "secret": secret, "bound": biz is not None}

    def listing(self) -> list[dict]:
        with self._lock:
            return [{"key_id": k, "origin": v["origin"], "label": v["label"], "created": v["created"], "business": v.get("business"), "expires": v.get("expires")} for k, v in self._data.items()]

    def remove(self, key_id: str) -> bool:
        with self._lock:
            gone = self._data.pop(key_id, None) is not None
            if gone:
                self._save()
            return gone

    def select(self, runtime: str, model: str, device: str = "auto") -> None:
        with self._lock:
            self.selected = {"runtime": runtime, "model": model, "device": device if device in ("auto", "cpu") else "auto"}
            self._save()

    def clear_selection(self) -> None:
        with self._lock:
            self.selected = {}
            self._save()

    def allowed_origins(self) -> set[str]:
        with self._lock:
            return {v["origin"] for v in self._data.values()}

    # ---- verifying a request
    def verify(self, method: str, path: str, body: bytes, headers: dict) -> tuple[str | None, str | None]:
        """(key_id, None) if the request is correctly signed, fresh and not a replay; else (None, reason). The reason is for the person at the companion, never a hint to an attacker (the HTTP
        answer is always the same 401)."""
        key_id, ts, nonce, sig = (headers.get(h, "") for h in ("X-Companion-Key", "X-Companion-Ts", "X-Companion-Nonce", "X-Companion-Sig"))
        with self._lock:
            entry = self._data.get(key_id)
            now = self.clock()
            if entry is None or not re.fullmatch(r"[0-9a-f]{64}", sig) or not re.fullmatch(r"[0-9A-Za-z_-]{16,64}", nonce) or not re.fullmatch(r"\d{10,13}(\.\d+)?", ts):
                return None, "malformed or unknown"
            if entry.get("expires") and now > entry["expires"]:
                return None, "expired"
            biz = entry.get("business")
            if biz and headers.get("X-Companion-Business", "") != biz:
                return None, "wrong business"  # a key made for one business cannot act for another
            t = float(ts) / (1000 if float(ts) > 1e11 else 1)
            if abs(now - t) > CLOCK_SKEW:
                return None, "stale timestamp"
            want = hmac.new(entry["secret"].encode(), "\n".join((method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest(), *([biz] if biz else []))).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, sig):
                return None, "bad signature"
            for k in [k for k, v in self._nonces.items() if v < now]:
                del self._nonces[k]
            if nonce in self._nonces:
                return None, "replay"
            self._nonces[nonce] = now + NONCE_TTL
            while len(self._nonces) > 4096:
                self._nonces.popitem(last=False)
            return key_id, None

    def origin_of(self, key_id: str) -> str | None:
        with self._lock:
            return (self._data.get(key_id) or {}).get("origin")


def host_ok(host_header: str | None, port: int) -> bool:
    """The Host header must name this computer's loopback address and this port: a name that resolves to 127.0.0.1 only after a DNS trick is refused."""
    if not host_header:
        return False
    return any(host_header == f"{h}:{port}" for h in HOSTS)


def sign(secret: str, method: str, path: str, ts: str, nonce: str, body: bytes, business: str | None = None) -> str:
    """What a client computes (tests and the companion's own tools use it; the browser does the same with WebCrypto)."""
    return hmac.new(secret.encode(), "\n".join((method.upper(), path, ts, nonce, hashlib.sha256(body).hexdigest(), *([business] if business else []))).encode(), hashlib.sha256).hexdigest()
