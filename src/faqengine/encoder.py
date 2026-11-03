"""Base embedding models we can fine-tune, and the encoder wrapper (tokenise, pool, normalise)."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn


@dataclass(frozen=True)
class BaseSpec:
    key: str
    hf_id: str
    pooling: str  # "mean" or "cls"
    query_prefix: str = ""
    doc_prefix: str = ""
    max_len: int = 128
    license: str = ""
    languages: str = "en"


# Permissive licences only: the customer receives and may redistribute the fine-tuned weights.
BASES: dict[str, BaseSpec] = {
    "minilm-l6": BaseSpec("minilm-l6", "sentence-transformers/all-MiniLM-L6-v2", "mean", license="apache-2.0"),
    "bge-small": BaseSpec("bge-small", "BAAI/bge-small-en-v1.5", "cls", license="mit"),
    "e5-small": BaseSpec("e5-small", "intfloat/e5-small-v2", "mean", "query: ", "passage: ", license="mit"),
    "multilingual-e5-small": BaseSpec(
        "multilingual-e5-small", "intfloat/multilingual-e5-small", "mean", "query: ", "passage: ", license="mit", languages="multilingual"
    ),
}


def _cached(model_id: str) -> bool:
    """True when a Hugging Face model id is already in the local cache."""
    if Path(model_id).is_dir():
        return False
    try:
        from huggingface_hub import constants

        snapshots = Path(constants.HF_HUB_CACHE) / ("models--" + model_id.replace("/", "--")) / "snapshots"
        return snapshots.is_dir() and any(snapshots.iterdir())
    except Exception:
        return False


def pick_device(name: str = "auto") -> torch.device:
    if name != "auto":
        return torch.device(name)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def release_memory() -> None:
    """Hand cached accelerator memory back. A run of several trainings in one process on an 8 GB machine otherwise ends in
    "Insufficient Memory" even though each training alone fits."""
    import gc

    gc.collect()
    try:
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001 - best effort; there is nothing useful to do if the driver refuses
        pass


class Encoder(nn.Module):
    """Text in, unit-length vector out. The same graph is what gets exported to ONNX."""

    def __init__(self, spec: BaseSpec, source: str | None = None, attn: str | None = None):
        super().__init__()
        from transformers import AutoModel, AutoTokenizer

        src = source or spec.hf_id
        self.spec = spec
        kwargs = {"attn_implementation": attn} if attn else {}
        for local_only in ([True, False] if _cached(src) else [False]):  # a cached model loads without touching the network
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(src, local_files_only=local_only)
                self.model = AutoModel.from_pretrained(src, local_files_only=local_only, **kwargs)
                break
            except OSError:
                if not local_only:
                    raise
        self.dim = int(self.model.config.hidden_size)

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        hidden = self.model(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
        if self.spec.pooling == "cls":
            pooled = hidden[:, 0]
        else:
            m = attention_mask.unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * m).sum(1) / m.sum(1).clamp(min=1e-9)
        return torch.nn.functional.normalize(pooled, dim=-1)

    def tokenize(self, texts: list[str], kind: str, device: torch.device) -> tuple[torch.Tensor, torch.Tensor]:
        prefix = self.spec.query_prefix if kind == "query" else self.spec.doc_prefix
        enc = self.tokenizer(
            [prefix + t for t in texts], padding=True, truncation=True, max_length=self.spec.max_len, return_tensors="pt",
            pad_to_multiple_of=16,  # few distinct shapes: Apple's GPU backend compiles and caches a graph per shape
        )
        return enc["input_ids"].to(device), enc["attention_mask"].to(device)

    @torch.no_grad()
    def encode(self, texts: list[str], kind: str = "doc", batch_size: int = 64, device: torch.device | None = None) -> np.ndarray:
        device = device or next(self.parameters()).device
        was_training = self.training
        self.eval()
        out = []
        # long-to-short ordering keeps padding small; undone below
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        for i in range(0, len(order), batch_size):
            ids, mask = self.tokenize([texts[j] for j in order[i:i + batch_size]], kind, device)
            out.append(self(ids, mask).float().cpu().numpy())
        self.train(was_training)
        if not out:
            return np.zeros((0, self.dim), dtype=np.float32)
        emb = np.concatenate(out)
        result = np.empty_like(emb)
        result[order] = emb
        return result

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        self.model.save_pretrained(path)
        self.tokenizer.save_pretrained(path)
        (path / "faqengine.json").write_text(json.dumps({"spec": asdict(self.spec)}, indent=2))

    @classmethod
    def load(cls, ref: str | Path, attn: str | None = None) -> "Encoder":
        """ref is a base key ('minilm-l6'), or a directory written by save()."""
        p = Path(str(ref))
        if p.is_dir() and (p / "faqengine.json").exists():
            spec = BaseSpec(**json.loads((p / "faqengine.json").read_text())["spec"])
            return cls(spec, source=str(p), attn=attn)
        if str(ref) not in BASES:
            raise ValueError(f"unknown base {ref!r}; choose from {', '.join(BASES)}")
        return cls(BASES[str(ref)], attn=attn)
