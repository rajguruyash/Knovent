"""The embedding model on its own: text in, unit vectors out. Used by the standalone engine and by the hosted service, so that both read a question the same way.
Needs only numpy, onnxruntime and tokenizers."""
from __future__ import annotations

import json
import threading
from collections import OrderedDict
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer


class Embedder:
    def __init__(self, model_dir: str | Path, threads: int = 0):
        model_dir = Path(model_dir)
        self.cfg = json.loads((model_dir / "engine.json").read_text())
        tok = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        tok.enable_truncation(max_length=self.cfg["max_len"])
        tok.enable_padding(pad_id=self.cfg["pad_id"], pad_token=self.cfg["pad_token"])
        self.tok = tok
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_dir / self.cfg["onnx"]), opts, providers=["CPUExecutionProvider"])
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._cache_lock = threading.Lock()
        self.cache_size = 20_000

    @property
    def dim(self) -> int:
        return int(self.cfg["dim"])

    def embed(self, texts: list[str], kind: str = "query", batch_size: int = 64) -> np.ndarray:
        prefix = self.cfg["query_prefix"] if kind == "query" else self.cfg["doc_prefix"]
        out = []
        for i in range(0, len(texts), batch_size):
            enc = self.tok.encode_batch([prefix + t for t in texts[i:i + batch_size]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            out.append(self.session.run(None, {"input_ids": ids, "attention_mask": mask})[0])
        return np.concatenate(out) if out else np.zeros((0, self.dim), np.float32)

    def embed_docs_cached(self, texts: list[str]) -> np.ndarray:
        """Document-side vectors with a memory of the ones already computed. The evidence check compares each question with the same stored sentences again and again; their vectors do not
        change, so they are computed once (the least recently used are forgotten beyond `cache_size`)."""
        out: list[np.ndarray | None] = [None] * len(texts)
        missing: list[int] = []
        with self._cache_lock:
            for i, t in enumerate(texts):
                hit = self._cache.get(t)
                if hit is None:
                    missing.append(i)
                else:
                    self._cache.move_to_end(t)
                    out[i] = hit
        if missing:
            fresh = self.embed([texts[i] for i in missing], "doc")
            with self._cache_lock:
                for i, v in zip(missing, fresh):
                    out[i] = v
                    self._cache[texts[i]] = v
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return np.stack(out) if out else np.zeros((0, self.dim), np.float32)  # type: ignore[arg-type]
