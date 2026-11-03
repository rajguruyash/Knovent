"""The model catalogue (catalogue.json), validated when read. Data, not code: it is edited by a person who read a provider's page, and the file says when and where. An unknown model is unknown (no
structured-output support, no price), never assumed from its neighbours."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

STRUCTURED = ("json_schema", "json_object", "unverified")
REQUIRED = ("provider", "id", "label", "structured", "verified_on", "source", "platform_offered")


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    id: str
    label: str
    structured: str
    context: int | None
    price_in_per_m: float | None  # USD per million tokens, as the provider listed it on `verified_on`
    price_out_per_m: float | None
    licence: str | None
    verified_on: str
    source: str
    tier: str | None  # free_trial | best_effort_free | paid, once someone has decided what the platform offers
    platform_offered: bool
    extra_body: dict
    notes: str

    def cost(self, tokens_in: int, tokens_out: int) -> float | None:
        if self.price_in_per_m is None or self.price_out_per_m is None:
            return None
        return tokens_in / 1e6 * self.price_in_per_m + tokens_out / 1e6 * self.price_out_per_m


def _path() -> Path:
    return Path(os.environ.get("MODEL_CATALOGUE_FILE") or Path(__file__).with_name("catalogue.json"))


@lru_cache(maxsize=8)
def _load(path: str, mtime: float) -> dict[tuple[str, str], ModelInfo]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[tuple[str, str], ModelInfo] = {}
    for m in data.get("models", []):
        missing = [k for k in REQUIRED if k not in m]
        if missing:
            raise ValueError(f"catalogue entry {m.get('id')!r} is missing {missing}")
        if m["structured"] not in STRUCTURED:
            raise ValueError(f"catalogue entry {m['id']!r}: structured must be one of {STRUCTURED}")
        if m.get("platform_offered") and (m.get("tier") not in ("free_trial", "best_effort_free", "paid") or m.get("price_in_per_m") is None or m.get("price_out_per_m") is None):
            raise ValueError(f"catalogue entry {m['id']!r} is offered by the platform but has no tier or no price: a model is not offered without a cost model")
        key = (m["provider"], m["id"])
        if key in out:
            raise ValueError(f"catalogue lists {key} twice")
        out[key] = ModelInfo(m["provider"], m["id"], m["label"], m["structured"], m.get("context"), m.get("price_in_per_m"), m.get("price_out_per_m"), m.get("licence"), m["verified_on"], m["source"],
                             m.get("tier"), bool(m["platform_offered"]), dict(m.get("extra_body") or {}), m.get("notes", ""))
    return out


def entries() -> dict[tuple[str, str], ModelInfo]:
    p = _path()
    return _load(str(p), p.stat().st_mtime)


def lookup(provider: str, model: str) -> ModelInfo | None:
    return entries().get((provider, model))


def platform_models() -> list[ModelInfo]:
    """What the platform offers to customers right now: only entries that were released on purpose (platform_offered, with a tier and a price)."""
    return [m for m in entries().values() if m.platform_offered]
