"""Encrypting a business's API key at rest: AES-256-GCM, with a key that lives only in the service's environment (never in the database, never in a migration).

The business id and the key version are bound to every ciphertext as authenticated data, so a stored key cannot be copied to another business's row and still decrypt, and a flipped bit
is detected. Rotation: set LLM_CREDENTIAL_KEYS="2:<new>,1:<old>" (the first is used to encrypt, all are tried by version); re-save a key to move it to the newest.
"""
from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE = 12


class SecretError(Exception):
    """The key could not be stored or recovered. Never contains the key or the plaintext."""


class SecretBox:
    def __init__(self, keys: dict[int, bytes], current: int | None = None):
        if not keys:
            raise SecretError("no credential key is configured")
        for v, k in keys.items():
            if len(k) != 32:
                raise SecretError(f"credential key {v} must be 32 bytes (base64 of 32 random bytes)")
        self.keys, self.current = keys, current if current is not None else max(keys)

    @classmethod
    def from_env(cls, env=os.environ) -> "SecretBox":
        raw = env.get("LLM_CREDENTIAL_KEYS", "").strip()
        if not raw:
            raise SecretError("set LLM_CREDENTIAL_KEYS to enable saving API keys (for example: 1:<base64 of 32 random bytes>)")
        keys: dict[int, bytes] = {}
        order: list[int] = []
        for part in raw.split(","):
            try:
                version, b64 = part.strip().split(":", 1)
                keys[int(version)] = base64.b64decode(b64, validate=True)
                order.append(int(version))
            except (ValueError, TypeError):
                raise SecretError("LLM_CREDENTIAL_KEYS is not in the form version:base64[,version:base64]") from None
        return cls(keys, order[0])

    @staticmethod
    def generate() -> str:
        return base64.b64encode(os.urandom(32)).decode()

    def encrypt(self, plaintext: str, business_id: str) -> tuple[bytes, int]:
        nonce = os.urandom(NONCE)
        ct = AESGCM(self.keys[self.current]).encrypt(nonce, plaintext.encode("utf-8"), f"{business_id}:{self.current}".encode())
        return nonce + ct, self.current

    def decrypt(self, blob: bytes, business_id: str, version: int) -> str:
        key = self.keys.get(version)
        if key is None:
            raise SecretError(f"the key used to encrypt this credential (version {version}) is no longer configured")
        try:
            return AESGCM(key).decrypt(bytes(blob[:NONCE]), bytes(blob[NONCE:]), f"{business_id}:{version}".encode()).decode("utf-8")
        except (InvalidTag, ValueError):
            raise SecretError("the stored credential could not be decrypted (wrong key, or it was changed or moved)") from None
