"""Contrastive fine-tuning of the embedding model on a FAQ knowledge base.

Each training example is a customer-style question and the entry it belongs to. The loss pulls it towards
one of that entry's views and away from the other entries' views in the batch, plus one hard negative
mined from the model itself (a confusable neighbouring FAQ). A batch never holds two examples of the
same entry, and any view of an example's own entry is masked out of its negatives, so a correct
answer is never punished.
"""
from __future__ import annotations

import copy
import json
import random
import resource
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import torch
import torch.nn.functional as F

from . import metrics
from .augment import augment
from .encoder import Encoder, pick_device
from .kb import KB, Queries


@dataclass
class TrainConfig:
    base: str = "minilm-l6"
    max_epochs: int = 60
    max_steps: int = 600
    fixed_steps: int = 60  # used when there is no dev set to stop on
    min_eval_steps: int = 8
    patience: int = 3  # evaluations without improvement
    batch_size: int = 32
    lr: float = 1e-4  # chosen on dev: scripts/sweep_hparams.py
    scale: float = 20.0  # 1 / temperature
    n_aug: int = 3
    positives: int = 1  # other phrasings of the same entry each question is pulled towards (1: the original recipe)
    negatives: int = 1  # hard negatives per question, taken from the most similar other entries
    aug_kinds: tuple[str, ...] | None = None  # None: every kind
    hard_negatives: bool = True
    warmup_ratio: float = 0.1
    seed: int = 0
    device: str = "auto"


Event = Callable[[dict], None]


def memory_mb(device: torch.device) -> float:
    if device.type == "mps":
        return torch.mps.current_allocated_memory() / 2**20
    if device.type == "cuda":
        return torch.cuda.memory_allocated() / 2**20
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (2**20 if sys.platform == "darwin" else 2**10)


def make_examples(kb: KB, n_aug: int, rng: random.Random, kinds: tuple[str, ...] | None = None) -> list[tuple[str, int]]:
    out = []
    for i, e in enumerate(kb.entries):
        for q in e.questions:
            out.append((q, i))
            out += [(a, i) for a in augment(q, rng, n_aug, kinds)]
    return out


def make_batches(examples: list[tuple[str, int]], size: int, rng: random.Random) -> list[list[int]]:
    """Shuffled batches in which every entry appears at most once."""
    pending = list(range(len(examples)))
    rng.shuffle(pending)
    batches = []
    while pending:
        batch, seen, rest = [], set(), []
        for idx in pending:
            entry = examples[idx][1]
            if entry in seen or len(batch) >= size:
                rest.append(idx)
            else:
                batch.append(idx)
                seen.add(entry)
        if len(batch) >= 2:
            batches.append(batch)
        pending = rest if len(batch) >= 2 else []
    rng.shuffle(batches)
    return batches


def mine_negatives(enc: Encoder, kb: KB, device: torch.device, top: int = 4) -> list[list[int]]:
    """For each entry, the entries whose centroid embedding is closest to its own."""
    V = enc.encode([v.text for v in kb.views], "doc", device=device)
    sums = np.add.reduceat(V, kb.view_starts, axis=0)
    C = sums / np.linalg.norm(sums, axis=1, keepdims=True).clip(min=1e-9)
    sim = C @ C.T
    np.fill_diagonal(sim, -np.inf)
    return [[int(j) for j in np.argsort(-row)[:top] if np.isfinite(row[j])] for row in sim]


def step_loss(enc: Encoder, kb: KB, examples, batch, negatives, cfg: TrainConfig, rng: random.Random, device) -> torch.Tensor | None:
    if cfg.positives > 1 or cfg.negatives > 1:
        return step_loss_multi(enc, kb, examples, batch, negatives, cfg, rng, device)
    queries, q_ent, pos, pos_ent, neg, neg_ent = [], [], [], [], [], []
    for idx in batch:
        text, ent = examples[idx]
        candidates = [v for v in kb.views_of(ent) if v.text != text]
        if not candidates:
            continue
        queries.append(text)
        q_ent.append(ent)
        pos.append(rng.choice(candidates).text)
        pos_ent.append(ent)
        if cfg.hard_negatives and negatives[ent]:
            other = rng.choice(negatives[ent])
            neg.append(rng.choice(kb.views_of(other)).text)
            neg_ent.append(other)
    n = len(queries)
    if n < 2:
        return None
    q = enc(*enc.tokenize(queries, "query", device))
    d = enc(*enc.tokenize(pos + neg, "doc", device))
    return contrastive_loss(q, d, q_ent, pos_ent + neg_ent, cfg.scale)


def contrastive_loss(q: torch.Tensor, d: torch.Tensor, q_ent: list[int], d_ent: list[int], scale: float) -> torch.Tensor:
    """Query i's right document is d[i]. Every other document is a negative, except those that belong to
    query i's own entry (a hard negative can be another view of an entry that is in the batch): they are masked."""
    n = len(q_ent)
    logits = scale * q @ d.T
    q_e = torch.tensor(q_ent, device=q.device)
    d_e = torch.tensor(d_ent, device=q.device)
    own = torch.zeros_like(logits, dtype=torch.bool)
    own[torch.arange(n), torch.arange(n)] = True
    logits = logits.masked_fill((q_e[:, None] == d_e[None, :]) & ~own, -1e4)
    return F.cross_entropy(logits, torch.arange(n, device=q.device))


def weight_change(before: dict, after: dict) -> dict:
    """How far `after` is from `before`: relative L2 distance over all weights, and how many of the weight tensors differ at all."""
    num = den = 0.0
    changed = total = 0
    for k, w0 in before.items():
        if not w0.is_floating_point():
            continue
        w1 = after[k]
        total += 1
        d = float(((w1.float() - w0.float()) ** 2).sum())
        num += d
        den += float((w0.float() ** 2).sum())
        changed += d > 0
    return {"relative_l2": (num / den) ** 0.5 if den else 0.0, "tensors_changed": changed, "tensors_total": total}


def dev_metrics(enc: Encoder, kb: KB, dev: Queries, device: torch.device, scale: float) -> dict[str, float]:
    V = enc.encode([v.text for v in kb.views], "doc", device=device)
    Q = enc.encode(dev.texts, "query", device=device)
    S = kb.entry_max(Q @ V.T)
    m = metrics.summarize(S, dev.gold)
    m["loss"] = metrics.listwise_loss(S, dev.gold, scale)
    return m


def train(
    kb: KB,
    dev: Queries | None,
    cfg: TrainConfig,
    out_dir: str | Path | None = None,
    on_event: Event | None = None,
    base: Encoder | None = None,
) -> tuple[Encoder, dict]:
    """Fine-tune and return (encoder, log). `base` is copied, never modified."""
    emit = on_event or (lambda e: None)
    random.seed(cfg.seed)
    np.random.seed(cfg.seed)
    torch.manual_seed(cfg.seed)
    rng = random.Random(cfg.seed)
    device = pick_device(cfg.device)

    enc = copy.deepcopy(base) if base is not None else Encoder.load(cfg.base)
    enc.to(device)
    t0 = time.time()
    emit({"event": "stage", "stage": "starting", "device": str(device), "entries": len(kb), "views": len(kb.views)})

    before = dev_metrics(enc, kb, dev, device, cfg.scale) if dev else None
    budget = cfg.max_steps if dev else cfg.fixed_steps
    optim = torch.optim.AdamW(enc.parameters(), lr=cfg.lr, weight_decay=0.01)
    warm = max(5, int(budget * cfg.warmup_ratio))
    sched = torch.optim.lr_scheduler.LambdaLR(
        optim, lambda s: (s + 1) / warm if s < warm else max(0.0, (budget - s) / max(1, budget - warm))
    )

    # The untuned weights are the starting "best": a checkpoint is only kept if it beats them on dev.
    untuned = {k: v.detach().cpu().clone() for k, v in enc.state_dict().items()} if dev else None
    best = {"mrr@10": before["mrr@10"] if before else -1.0, "step": 0, "state": untuned}
    curve, window, since_eval, bad, step, epochs_run = [], [], 0, 0, 0, 0
    enc.train()
    stop = False
    for epoch in range(1, cfg.max_epochs + 1):
        emit({"event": "stage", "stage": "mining", "epoch": epoch})
        negatives = mine_negatives(enc, kb, device) if cfg.hard_negatives else [[] for _ in kb.entries]
        enc.train()
        examples = make_examples(kb, cfg.n_aug, random.Random(cfg.seed * 1000 + epoch), cfg.aug_kinds)
        batches = make_batches(examples, cfg.batch_size, rng)
        epochs_run = epoch
        for batch in batches:
            started = time.time()
            loss = step_loss(enc, kb, examples, batch, negatives, cfg, rng, device)
            if loss is None:
                continue
            optim.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(enc.parameters(), 1.0)
            optim.step()
            sched.step()
            step += 1
            since_eval += 1
            window.append(loss.item())
            dt = time.time() - started
            emit({
                "event": "step", "epoch": epoch, "step": step, "of": budget, "loss": window[-1],
                "lr": sched.get_last_lr()[0], "samples_per_s": len(batch) / max(dt, 1e-9),
                "elapsed_s": time.time() - t0, "eta_s": (budget - step) * (time.time() - t0) / step,
                "mem_mb": memory_mb(device),
            })
            if step >= budget:
                stop = True
                break
        if dev and (since_eval >= cfg.min_eval_steps or stop or epoch == cfg.max_epochs):
            m = dev_metrics(enc, kb, dev, device, cfg.scale)
            curve.append({"step": step, "epoch": epoch, "train_loss": float(np.mean(window)), **{f"dev_{k}": v for k, v in m.items()}})
            emit({"event": "eval", "step": step, "epoch": epoch, "train_loss": float(np.mean(window)), "dev": m})
            window, since_eval = [], 0
            if m["mrr@10"] > best["mrr@10"] + 1e-4:
                best.update({"mrr@10": m["mrr@10"], "step": step, "state": {k: v.detach().cpu().clone() for k, v in enc.state_dict().items()}})
                bad = 0
            else:
                bad += 1
                if bad >= cfg.patience:
                    emit({"event": "stage", "stage": "early_stop", "step": step})
                    stop = True
        if stop:
            break

    final_loss = float(np.mean(window)) if window else (curve[-1]["train_loss"] if curve else float("nan"))
    trained_change = weight_change(untuned, {k: v.detach().cpu() for k, v in enc.state_dict().items()}) if untuned else None  # where the last step left the weights
    if dev:
        enc.load_state_dict(best["state"])  # the best checkpoint, or the untuned weights if nothing beat them
    enc.eval()
    after = dev_metrics(enc, kb, dev, device, cfg.scale) if dev else None
    log = {
        "config": asdict(cfg), "device": str(device), "steps": step, "epochs": epochs_run, "best_step": best["step"],
        "train_seconds": time.time() - t0, "train_loss_last": final_loss, "dev_before": before, "dev_after": after,
        "beat_untuned_on_dev": bool(dev and best["step"] > 0), "curve": curve,
        # proof that fine-tuning really changed the model: how far the weights moved (relative L2 distance from the pretrained ones), for the weights training ended
        # on and for the checkpoint that was kept (0 when nothing beat the pretrained model, in which case the pretrained model is what ships)
        "weights": {"after_last_step": trained_change,
                    "kept": weight_change(untuned, {k: v.detach().cpu() for k, v in enc.state_dict().items()}) if untuned else None},
    }
    if out_dir:
        out = Path(out_dir)
        enc.save(out / "model")
        (out / "train_log.json").write_text(json.dumps(log, indent=2))
    emit({"event": "stage", "stage": "done", "train_seconds": log["train_seconds"]})
    return enc, log


def step_loss_multi(enc: Encoder, kb: KB, examples, batch, negatives, cfg: TrainConfig, rng: random.Random, device) -> torch.Tensor | None:
    """Each question gets `positives` other phrasings of its entry and `negatives` views of the entries most like it."""
    P, N = cfg.positives, cfg.negatives if cfg.hard_negatives else 0
    queries, q_ent, pos, pos_ent, neg, neg_ent = [], [], [], [], [], []
    for idx in batch:
        text, ent = examples[idx]
        candidates = [v.text for v in kb.views_of(ent) if v.text != text]
        if not candidates:
            continue
        chosen = rng.sample(candidates, min(P, len(candidates)))
        chosen += [rng.choice(candidates) for _ in range(P - len(chosen))]  # an entry with few phrasings repeats some
        queries.append(text)
        q_ent.append(ent)
        pos += chosen
        pos_ent += [ent] * P
        pool = negatives[ent]
        for other in (rng.sample(pool, min(N, len(pool))) if pool else []):
            neg.append(rng.choice(kb.views_of(other)).text)
            neg_ent.append(other)
    if len(queries) < 2:
        return None
    q = enc(*enc.tokenize(queries, "query", device))
    d = enc(*enc.tokenize(pos + neg, "doc", device))
    return multi_positive_loss(q, d, q_ent, pos_ent + neg_ent, P, cfg.scale)


def multi_positive_loss(q: torch.Tensor, d: torch.Tensor, q_ent: list[int], d_ent: list[int], positives: int, scale: float) -> torch.Tensor:
    """Docs 0..B*P-1 are the positives (query i owns docs i*P .. i*P+P-1); the rest are hard negatives. For every positive the
    contest is that positive against all documents of OTHER entries, so the other positives of the same entry are never
    treated as negatives."""
    n = len(q_ent)
    logits = scale * q @ d.T
    same = torch.tensor(q_ent, device=q.device)[:, None] == torch.tensor(d_ent, device=q.device)[None, :]
    lse_neg = torch.logsumexp(logits.masked_fill(same, float("-inf")), dim=1, keepdim=True)
    pos_idx = torch.arange(n, device=q.device)[:, None] * positives + torch.arange(positives, device=q.device)[None, :]
    pos_logits = logits.gather(1, pos_idx)
    return -(pos_logits - torch.logaddexp(pos_logits, lse_neg)).mean()
