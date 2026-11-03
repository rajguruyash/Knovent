"""A pretrained cross-encoder as a reranker, and the bookkeeping around it. It reads a question and one candidate together and returns a relevance score: a number for
ordering candidates against each other for ONE question. It is not a probability, not an answer confidence, and not comparable between questions."""
from __future__ import annotations

import numpy as np
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODELS = {  # the lightweight candidates that were downloaded and measured
    "tinybert-l2": "cross-encoder/ms-marco-TinyBERT-L-2-v2",
    "minilm-l6": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    "minilm-l12": "cross-encoder/ms-marco-MiniLM-L-12-v2",
    "mmarco-mminilm": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
}
TOP = 20  # how many candidates the retrieval engine hands to the reranker


class CrossEncoder:
    def __init__(self, model_id: str, device: str = "cpu", max_len: int = 256):
        self.id, self.device, self.max_len = model_id, torch.device(device), max_len
        self.tok = AutoTokenizer.from_pretrained(model_id, local_files_only=True)  # offline: the files must already be on this machine
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id, local_files_only=True).eval()
        for p in self.model.parameters():  # copy the weights out of the memory-mapped file: on this machine a matmul on the mapped storage returned NaN, on a copy it does not
            p.data = p.data.clone()
        self.model.to(self.device)

    @torch.no_grad()
    def score(self, query: str, texts: list[str], batch: int = 32) -> np.ndarray:
        out = []
        for i in range(0, len(texts), batch):
            enc = self.tok([query] * len(texts[i:i + batch]), texts[i:i + batch], padding=True, truncation="only_second", max_length=self.max_len, return_tensors="pt").to(self.device)
            logits = self.model(**enc).logits
            out.append((logits[:, 0] if logits.shape[1] == 1 else logits[:, -1]).float().cpu().numpy())
        return np.concatenate(out) if out else np.zeros(0)


def candidate_text(entry) -> str:
    """What the reranker reads for a candidate: a FAQ entry's question and answer, or a passage's heading and text."""
    return f"{entry.question}. {entry.answer}" if entry.answer else entry.question


def order(scores: np.ndarray) -> np.ndarray:
    """Entry indices best first; ties broken by index so that the order is reproducible."""
    return np.lexsort((np.arange(len(scores)), -scores))


def rerank(base_scores: np.ndarray, ce_scores: np.ndarray, top: int = TOP) -> dict:
    """Reorder the first `top` of the retrieval order by the reranker; everything below keeps its retrieval order after them. Returns the final order and, for each candidate,
    its original rank and similarity, its reranker score and its final rank (the record the experiment keeps)."""
    base = order(base_scores)
    cand = base[:top]
    new = cand[order(ce_scores[: len(cand)])]
    final = np.concatenate([new, base[top:]])
    rows = []
    for orig_rank, e in enumerate(cand, start=1):
        rows.append({"entry": int(e), "original_rank": orig_rank, "original_score": float(base_scores[e]), "reranker_score": float(ce_scores[orig_rank - 1]),
                     "final_rank": int(np.flatnonzero(final == e)[0]) + 1})
    return {"final": final, "candidates": rows}


def metrics_for(ranking: np.ndarray, gold: frozenset) -> dict:
    """Per-question metrics from a ranked list of entry indices and the set of right entries."""
    pos = [int(np.flatnonzero(ranking == g)[0]) + 1 for g in gold]
    out = {f"recall@{k}": sum(p <= k for p in pos) / len(gold) for k in (5, 10, 20)}
    first = min(pos)
    out["right_first"] = float(ranking[0] in gold)
    out["mrr@10"] = 1.0 / first if first <= 10 else 0.0
    dcg = sum(1.0 / np.log2(p + 1) for p in pos if p <= 10)
    out["ndcg@10"] = dcg / sum(1.0 / np.log2(i + 1) for i in range(1, min(len(gold), 10) + 1))
    out["gold_in_top20"] = float(all(p <= 20 for p in pos))
    out["best_gold_rank"] = first
    return out
