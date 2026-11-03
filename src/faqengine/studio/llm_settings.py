"""Each business (here: each project) chooses whether to use a language model, which provider and model, and keeps its own key. The settings live in the project's folder, in a file only
the person running the Studio can read (mode 0600). They are never part of an exported package, a project listing or any response: the key is reported as present or absent.

Local mode stores the key in that file as it is. That protects it from other users of the computer, not from someone who has the whole disk; a hosted deployment encrypts it instead
(hosted/secrets.py). The page says which one applies.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..runtime import llm as L

FILE = "llm.json"
FIELDS = ("enabled", "provider", "model", "api_key", "base_url", "polish", "grounded", "assist", "onboarded")


class LLMSettings:
    def __init__(self, project_dir):
        self.path = Path(project_dir) / FILE

    def _read(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".llm-")
        try:
            os.fchmod(fd, 0o600)  # before anything is written to it
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def config(self) -> L.LLMConfig:
        d = self._read()
        return L.LLMConfig.from_dict({k: d[k] for k in FIELDS if k in d and k != "onboarded"})

    def public(self) -> dict:
        """What the page may see. `onboarded` is whether the owner has answered the question yet; `storage` says how the key is kept."""
        cfg = self.config()
        return {**cfg.public(), "onboarded": bool(self._read().get("onboarded")), "storage": "a file only you can read (not encrypted)", "usable": cfg.enabled and cfg.problem() is None,
                "problem": cfg.problem()}

    def clean(self, body: dict) -> dict:
        """The new settings from a request, checked. A missing or empty `api_key` keeps the stored one (the page never has it to send back)."""
        cur = self._read()
        new = {k: cur[k] for k in FIELDS if k in cur}
        if "provider" in body:
            if body["provider"] not in L.PROVIDERS and body["provider"] != "":
                raise ValueError("Unknown provider.")
            if body["provider"] != new.get("provider"):
                new.pop("model", None)  # a model belongs to one provider
                if not body.get("api_key"):
                    new.pop("api_key", None)  # and so does a key: it is never carried to another provider
            new["provider"] = body["provider"]
        for k in ("model", "base_url"):
            if k in body:
                if not isinstance(body[k], str) or len(body[k]) > 300:
                    raise ValueError(f"{k} must be text.")
                new[k] = body[k].strip()
        if body.get("api_key"):
            if not isinstance(body["api_key"], str) or not 8 <= len(body["api_key"].strip()) <= 400:
                raise ValueError("That does not look like an API key.")
            new["api_key"] = body["api_key"].strip()
        if body.get("clear_key"):
            new.pop("api_key", None)
        for k in ("enabled", "polish", "grounded", "assist"):
            if k in body:
                if not isinstance(body[k], bool):
                    raise ValueError(f"{k} must be true or false.")
                new[k] = body[k]
        if new.get("base_url") and not new["base_url"].startswith(("http://", "https://")):
            raise ValueError("The server address must start with http:// or https://")
        return new

    def save(self, body: dict) -> dict:
        new = self.clean(body)
        if new.get("enabled"):
            why = L.LLMConfig.from_dict({k: new[k] for k in FIELDS if k in new and k != "onboarded"}).problem()
            if why:
                raise ValueError(f"Cannot turn it on yet: {why}.")
        new["onboarded"] = True  # the question has been answered, either way
        self._write(new)
        return self.public()

    def skip(self) -> dict:
        """'Continue in local-only mode': answers the onboarding question without a provider."""
        d = self._read()
        d["enabled"], d["onboarded"] = False, True
        self._write(d)
        return self.public()

    def validate(self, body: dict) -> dict:
        """Checks what the page typed (with the stored key if it typed none) without saving it. Never raises on a provider problem."""
        try:
            new = self.clean(body)
        except ValueError as e:
            return {"ok": False, "code": "invalid", "message": str(e), "models": [], "checked": []}
        cfg = L.LLMConfig.from_dict({k: new[k] for k in FIELDS if k in new and k != "onboarded"})
        return L.validate(cfg, allow_private=True)  # a business running the Studio on its own computer may use a local model server

    def remove(self) -> None:
        self.path.unlink(missing_ok=True)

    def connection(self) -> L.LLM | None:
        cfg = self.config()
        return L.LLM(cfg, allow_private=True) if cfg.enabled and cfg.problem() is None else None
