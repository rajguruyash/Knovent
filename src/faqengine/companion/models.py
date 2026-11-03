"""WHICH LOCAL MODELS FIT THIS MACHINE, said honestly. Memory needs are ESTIMATED from the parameter count and the quantisation (weights plus working memory), not measured; the verdicts are about whether
a model fits, never about how good or how fast it is. Three verdicts:

    comfortable   the model needs at most 30% of the memory it would run in when that memory is SHARED with the system and the browser (RAM, Apple silicon), or 60% of a GPU's own memory
    possible      up to 50% of shared memory, or 85% of a GPU's own: it can run, slowly, and only with other programs closed (an 8 GB laptop with a 3B model was in this class and ran too slowly to benchmark)
    not_advised   more than that: it would swap or fail

and where it runs: on a GPU whose memory is known (NVIDIA VRAM, or Apple silicon's shared memory), otherwise on the CPU, which works but is much slower. Nobody is assumed to own a GPU."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .hardware import Hardware

BYTES_PER_PARAM = {"q4_k_m": 0.60, "q4_0": 0.56, "q5_k_m": 0.70, "q8_0": 1.07, "fp16": 2.0}
SHARED = (0.30, 0.50)  # (comfortable, possible) as a share of memory the system also uses: RAM, or Apple silicon's unified memory
DEDICATED = (0.60, 0.85)  # ... of a discrete GPU's own memory
OVERHEAD_GB = 1.0  # runtime, context and working memory: a flat allowance, not a measurement


@dataclass(frozen=True)
class LocalModel:
    id: str
    family: str
    params_b: float
    quant: str
    ollama: str | None
    gguf: dict | None
    context: int
    licence: str | None
    verified_on: str | None
    notes: str


def catalogue(path: str | None = None) -> list[LocalModel]:
    p = Path(path or os.environ.get("COMPANION_MODELS_FILE") or Path(__file__).with_name("catalogue.json"))
    out = []
    for m in json.loads(p.read_text(encoding="utf-8")).get("models", []):
        if m.get("quant") not in BYTES_PER_PARAM or not isinstance(m.get("params_b"), (int, float)) or m["params_b"] <= 0:
            raise ValueError(f"local model {m.get('id')!r}: a known quantisation and a positive parameter count are needed")
        out.append(LocalModel(m["id"], m.get("family", ""), float(m["params_b"]), m["quant"], m.get("ollama"), m.get("gguf"), int(m.get("context", 8192)), m.get("licence"), m.get("verified_on"), m.get("notes", "")))
    return out


def speed_class(m: LocalModel) -> str:
    """A rule of thumb from the parameter count, NOT a measurement: smaller models answer faster and reason less well. `fastest` (about 1 billion parameters or fewer), `fast` (up to about 2),
    `balanced` (up to about 4), `capable` (larger: slower, needs more memory)."""
    return "fastest" if m.params_b <= 1.2 else "fast" if m.params_b <= 2.2 else "balanced" if m.params_b <= 4.2 else "capable"


def estimate_gb(m: LocalModel) -> float:
    return round(m.params_b * BYTES_PER_PARAM[m.quant] * 1.1 + OVERHEAD_GB, 1)


def assess(m: LocalModel, hw: Hardware) -> dict:
    need = estimate_gb(m)
    gpu_gb = hw.best_accelerator_gb
    if any(g.unified and g.usable_for_models for g in hw.gpus):
        where, pool, limits = "gpu", hw.ram_gb, SHARED  # Apple silicon: the GPU and the CPU share one memory, so the fit is judged against all of it
    elif gpu_gb and need <= gpu_gb * DEDICATED[1]:
        where, pool, limits = "gpu", gpu_gb, DEDICATED
    else:
        where, pool, limits = "cpu", hw.ram_gb, SHARED
    if pool is None:
        return {**asdict(m), "speed": speed_class(m), "needs_gb": need, "runs_on": "unknown", "verdict": "unknown", "why": "this machine's memory could not be read, so nothing can be said about fit"}
    ratio = need / pool
    verdict = "comfortable" if ratio <= limits[0] else "possible" if ratio <= limits[1] else "not_advised"
    why = {"comfortable": f"needs about {need} GB of the {pool} GB it would run in", "possible": f"needs about {need} GB of {pool} GB: it will run slowly and only with other programs closed",
           "not_advised": f"needs about {need} GB but only {pool} GB is available there: it would swap or fail"}[verdict]
    if where == "cpu" and verdict != "not_advised":
        why += "; on the CPU (no GPU memory is known to be usable), answers take seconds to minutes"
    return {**asdict(m), "speed": speed_class(m), "needs_gb": need, "runs_on": where, "verdict": verdict, "why": why}


def recommend(hw: Hardware, models: list[LocalModel] | None = None) -> dict:
    """Every candidate assessed, best first (the largest that is comfortable, then the largest that is possible), the limits of this machine in words, and `suggested`: the pick for each speed
    class that this machine can run (comfortable first, otherwise possible), so that a person is offered several small, fast options and not one answer. `start_with` is the one to download first
    when nothing is installed: the most capable model that is comfortable and not larger than `balanced`; failing that, the fastest one that is possible; None when nothing fits."""
    rows = [assess(m, hw) for m in (models if models is not None else catalogue())]
    rank = {"comfortable": 0, "possible": 1, "not_advised": 2, "unknown": 3}
    rows.sort(key=lambda r: (rank[r["verdict"]], -r["params_b"]))
    limits = list(hw.notes)
    if hw.ram_gb is not None and hw.ram_gb < 12:
        limits.append(f"with {hw.ram_gb} GB of memory only the smallest models are realistic, and answers can still take minutes; hosted modes may serve this business better")
    runnable = [r for r in rows if r["verdict"] in ("comfortable", "possible")]
    if not runnable:
        limits.append("no candidate model fits this machine: local mode is not advised here")
    suggested: dict[str, str] = {}
    for cls in ("fastest", "fast", "balanced", "capable"):
        pick = [r for r in runnable if r["speed"] == cls]
        pick.sort(key=lambda r: (rank[r["verdict"]], -r["params_b"]))
        if pick:
            suggested[cls] = pick[0]["id"]
    comfy = [r for r in rows if r["verdict"] == "comfortable" and r["speed"] != "capable" and r.get("ollama")]
    possible = sorted((r for r in rows if r["verdict"] == "possible" and r.get("ollama")), key=lambda r: r["params_b"])
    start = max(comfy, key=lambda r: r["params_b"]) if comfy else (possible[0] if possible else None)
    return {"hardware": hw.public(), "models": rows, "limits": limits, "suggested": suggested, "start_with": start["id"] if start else None}
