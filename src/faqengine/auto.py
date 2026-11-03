"""FAQ file in, trained engine and report out. No held-out questions needed: they are generated (see selfcheck.py)."""
from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

import numpy as np

from .augment import TRAIN_KINDS
from .encoder import Encoder, pick_device
from .hardware import hardware
from . import language
from .heldout import load_heldout
from .heldout_report import build_heldout_report
from .kb import KB, Entry, Queries, load_kb, write_kb
from .report import build_report, render_markdown
from .selfcheck import build_selfcheck
from .train import TrainConfig, train

MIN_DEV = 10  # fewer generated dev questions than this cannot say whether tuning helped


def jsonable(x):
    """Plain Python for json: numpy numbers become numbers, NaN and infinity become null (the browser's JSON.parse rejects them)."""
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if hasattr(x, "tolist"):
        return jsonable(x.tolist())
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    return x


def note_language(report: dict, kb: KB, base_key: str) -> None:
    """When the text is in a language the model was not trained on, say so in the verdict (the first thing anyone reads)."""
    from .encoder import BASES

    note = language.advice(language.detect_kb(kb), BASES[base_key].languages)
    if note:
        report["verdict"].insert(1, note)


def with_extras(entries: list[Entry], extra: dict[str, list[str]]) -> list[Entry]:
    """The entries with extra phrasings appended (for example questions a person approved or corrected while testing)."""
    out = []
    for e in entries:
        have = {q.lower() for q in e.questions}
        out.append(Entry(e.id, [*e.questions, *[q for q in extra.get(e.id, []) if q.lower() not in have]], e.answer, e.category, e.kind, e.source))
    return out


def run_auto(kb_path: str | Path | list, run_dir: str | Path, on_event: Callable[[dict], None] | None = None, device: str = "auto",
             base_key: str = "minilm-l6", seed: int = 0, extra: dict[str, list[str]] | None = None, heldout_path: str | Path | None = None,
             floor: float | None = None, direct: float | None = None, finetune: bool = True) -> dict:
    """Train on the FAQ, check the result, and write everything the website and the exporter need into run_dir.

    With `heldout_path` (independent typed questions, see heldout.py) the engine is evaluated on those, with refusal validated separately;
    without it, checks are generated from the FAQ and are only sanity checks. `floor` and `direct` override the confidence thresholds."""
    emit = on_event or (lambda e: None)
    run = Path(run_dir)
    run.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    extra = extra or {}
    if heldout_path:
        return _run_heldout(kb_path, run, emit, device, base_key, seed, extra, heldout_path, floor, direct, finetune)

    emit({"event": "stage", "stage": "reading"})
    kb_file = load_kb(kb_path)
    kb_final = KB(with_extras(kb_file.entries, extra), kb_file.warnings)  # FAQ entries and documents, all searched
    faq_file, _ = kb_file.faq_only()
    if faq_file is None:
        return _run_documents(kb_final, run, emit, device, base_key, floor)
    sc = build_selfcheck(faq_file, seed)  # checks are generated for the FAQ entries; passages have no questions to generate from
    sc = replace(sc, kb=KB(with_extras(sc.kb.entries, extra)))  # approved phrasings are always trained on and indexed

    emit({"event": "stage", "stage": "loading_model"})
    base = Encoder.load(base_key)
    dev_t = pick_device(device)
    peak = [0.0]

    def watch(e: dict) -> None:
        peak[0] = max(peak[0], e.get("mem_mb") or 0.0)
        emit(e)

    n_dev = int((sc.dev.gold >= 0).sum())
    if n_dev < MIN_DEV or not finetune:
        tuned = base
        log = {"skipped": "fine-tuning was switched off." if not finetune else f"the FAQ is too small to generate enough checks ({n_dev} here, {MIN_DEV} needed) to tell whether tuning would help.",
               "steps": 0, "epochs": 0, "best_step": 0, "train_seconds": 0.0, "beat_untuned_on_dev": False, "dev_before": None, "dev_after": None,
               "train_loss_last": None, "curve": [], "device": str(dev_t), "config": {"base": base_key}}
        base.save(run / "model")
        (run / "train_log.json").write_text(json.dumps(log))
    else:
        cfg = TrainConfig(base=base_key, device=device, seed=seed, aug_kinds=TRAIN_KINDS)
        tuned, log = train(sc.kb, sc.dev, cfg, run, watch, base=base)

    emit({"event": "stage", "stage": "checking"})
    report, thresholds, V = build_report(kb_final, sc, base, tuned, log, dev_t, time.time() - t0, peak[0], hardware(str(run)))
    systems = report["selfcheck"]["systems"]
    if log.get("beat_untuned_on_dev") and (systems["tuned"]["hit@1"] < systems["base"]["hit@1"] or systems["tuned"]["mrr@10"] < systems["base"]["mrr@10"]):
        # chosen on a few dev questions, but worse on the check questions: ship the starting model, as the held-out path does
        log = {**log, "beat_untuned_on_dev": False, "worse_on_check": [systems["tuned"]["hit@1"], systems["base"]["hit@1"]]}
        tuned = base
        base.save(run / "model")
        (run / "train_log.json").write_text(json.dumps(jsonable(log)))
        report, thresholds, V = build_report(kb_final, sc, base, base, log, dev_t, time.time() - t0, peak[0], hardware(str(run)))

    note_language(report, kb_final, base_key)
    emit({"event": "stage", "stage": "saving"})
    np.save(run / "views.npy", V.astype(np.float32))
    write_kb(kb_final.entries, run / "kb.json")
    (run / "thresholds.json").write_text(json.dumps(jsonable(thresholds)))
    (run / "thresholds.auto.json").write_text(json.dumps(jsonable(thresholds)))  # what was calculated, to go back to
    (run / "selfcheck.json").write_text(json.dumps(jsonable({
        "dev": [{"query": t, "entry": None if g < 0 else sc.kb.entries[g].id, "source": s} for t, g, s in zip(sc.dev.texts, sc.dev.gold, sc.dev_source)],
        "test": [{"query": t, "entry": None if g < 0 else sc.kb.entries[g].id, "source": s} for t, g, s in zip(sc.test.texts, sc.test.gold, sc.test_source)]})))
    (run / "report.json").write_text(json.dumps(jsonable(report)))
    (run / "report.md").write_text(render_markdown(report))
    emit({"event": "stage", "stage": "finished"})
    return report


def export_run(run_dir: str | Path, out_dir: str | Path, name: str | None = None, thresholds: dict | None = None,
               sources: list[str | Path] | None = None, heldout: str | Path | None = None, web: list[dict] | None = None,
               regression: str | Path | None = None) -> dict:
    """Make the portable package from a finished run. `thresholds` (for example recalibrated from a person's tests) overrides the saved ones.
    `sources` and `heldout` are the files the run was made from: they go into the package so it can be retrained later (ci/improve.py)."""
    from .export import export_package

    run = Path(run_dir)
    kb = load_kb(run / "kb.json")
    saved = json.loads((run / "selfcheck.json").read_text())
    ids = {e.id: i for i, e in enumerate(kb.entries)}
    dev = Queries([r["query"] for r in saved["dev"]], np.array([ids.get(r["entry"], -1) for r in saved["dev"]], dtype=int))
    th = thresholds or json.loads((run / "thresholds.json").read_text())
    report = json.loads((run / "report.json").read_text())
    return export_package(
        run, kb, out_dir, dev, [r["query"] for r in saved["test"]], None, name=name or Path(out_dir).name,
        thresholds_override={"direct": th.get("direct"), "floor": th["floor"], "direct_reason": th.get("direct_reason")},
        extra_files={"eval/report.md": (run / "report.md").read_text(), "eval/report.json": json.dumps(jsonable(report))},
        sources=sources, heldout=heldout, web=web, regression=regression)


def _run_heldout(kb_path, run: Path, emit, device: str, base_key: str, seed: int, extra: dict, heldout_path, floor, direct, finetune: bool = True) -> dict:
    """Three kinds of data, kept apart. TRAIN: the FAQ's phrasings. VALIDATION: checks generated from the FAQ itself (a few phrasings held back, typos and
    shortened questions, answer keywords, generic off-topic questions); they stop training, choose between the pretrained and the fine-tuned model and place the
    "not in the FAQ" line. TEST: the held-out file, every row of it, used for nothing but measuring: it is never indexed, trained on, calibrated on or used to choose."""
    from .heldout import training_phrasings
    from .selfcheck import ANSWER_WORDS, HELD_OUT, OFF_TOPIC_SOURCE, SHORTENED

    t0 = time.time()
    emit({"event": "stage", "stage": "reading"})
    file_kb = load_kb(kb_path)
    kb = KB(with_extras(file_kb.entries, extra), file_kb.warnings)  # what is searched: the FAQ's phrasings and the documents. Nothing from the held-out file.
    faq_kb, keep = kb.faq_only()
    remap = {full: sub for sub, full in enumerate(keep)}
    sc = None
    if faq_kb is not None:
        sc = build_selfcheck(faq_kb, seed)
        sc = replace(sc, kb=KB(with_extras(sc.kb.entries, extra)))  # trained on and searched while validating; the held-back phrasings return for the final index
    ho = load_heldout(heldout_path, kb, train=training_phrasings(kb) + (sc.dev.texts if sc else []))  # a test question that copies a training or validation question is dropped
    ho.file_counts = ho.counts()  # what the file contained, before validation rows (never from the file) are put beside it
    if sc is not None:
        val_entries = [e if e.kind != "faq" else sc.kb.entries[remap[i]] for i, e in enumerate(kb.entries)]
        ho.val_kb = KB(val_entries)
        kinds = {HELD_OUT: "paraphrase", SHORTENED: "short", ANSWER_WORDS: "answer-terms", OFF_TOPIC_SOURCE: "out-of-scope-far"}
        for text, g, src in zip(sc.dev.texts, sc.dev.gold, sc.dev_source):
            ho.queries.append(text)
            ho.gold.append(frozenset() if g < 0 else frozenset({keep[int(g)]}))
            ho.types.append(kinds[src])
            ho.split.append("dev")
            ho.source.append("validation")

    emit({"event": "stage", "stage": "loading_model"})
    base = Encoder.load(base_key)
    dev_t = pick_device(device)
    peak = [0.0]

    def watch(e: dict) -> None:
        peak[0] = max(peak[0], e.get("mem_mb") or 0.0)
        emit(e)

    n_dev = int((sc.dev.gold >= 0).sum()) if sc else 0
    if sc is None or n_dev < MIN_DEV or not finetune:
        tuned = base
        why = ("fine-tuning was switched off" if not finetune else "there are no FAQ question and answer pairs to train on" if sc is None
               else f"the FAQ is too small to generate enough validation questions ({n_dev} here, {MIN_DEV} needed) to tell whether training helps")
        log = {"skipped": why + ".", "steps": 0, "epochs": 0, "best_step": 0, "train_seconds": 0.0, "beat_untuned_on_dev": False, "dev_before": None, "dev_after": None,
               "train_loss_last": None, "curve": [], "device": str(dev_t), "config": {"base": base_key}}
        base.save(run / "model")
        (run / "train_log.json").write_text(json.dumps(log))
    else:
        cfg = TrainConfig(base=base_key, device=device, seed=seed, n_aug=1, positives=2, negatives=2)
        tuned, log = train(sc.kb, sc.dev, cfg, run, watch, base=base)

    emit({"event": "stage", "stage": "checking"})
    report, thresholds, V, shipped = build_heldout_report(kb, ho, base, tuned, log, dev_t, time.time() - t0, peak[0], hardware(str(run)), floor, direct)
    if report["heldout"]["decision"]["ship"] == "base":
        base.save(run / "model")  # train() saved its best checkpoint; the decision is the unchanged pretrained model

    note_language(report, kb, base_key)
    emit({"event": "stage", "stage": "saving"})
    np.save(run / "views.npy", V.astype(np.float32))
    write_kb(kb.entries, run / "kb.json")
    (run / "thresholds.json").write_text(json.dumps(jsonable(thresholds)))
    (run / "thresholds.auto.json").write_text(json.dumps(jsonable(thresholds)))  # what was calculated, to go back to
    ids = [e.id for e in kb.entries]
    row = lambda i: {"query": ho.queries[i], "entry": ids[next(iter(ho.gold[i]))] if len(ho.gold[i]) == 1 else None, "source": ho.types[i]}  # noqa: E731
    (run / "selfcheck.json").write_text(json.dumps(jsonable({"dev": [row(i) for i in ho.where("dev")], "test": [row(i) for i in ho.where("test")]})))
    (run / "report.json").write_text(json.dumps(jsonable(report)))
    (run / "report.md").write_text(render_markdown(report))
    emit({"event": "stage", "stage": "finished"})
    return report


def _run_documents(kb: KB, run: Path, emit, device: str, base_key: str, floor: float | None) -> dict:
    """Only documents: the base model searches them. There is nothing to train on and nothing to check against, and the report says so."""
    from .report import build_documents_report

    t0 = time.time()
    emit({"event": "stage", "stage": "loading_model"})
    base = Encoder.load(base_key)
    dev_t = pick_device(device)
    base.save(run / "model")
    (run / "train_log.json").write_text(json.dumps({"skipped": "no FAQ pairs", "steps": 0, "epochs": 0, "best_step": 0, "train_seconds": 0.0, "beat_untuned_on_dev": False,
                                                    "dev_before": None, "dev_after": None, "train_loss_last": None, "curve": [], "device": str(dev_t), "config": {"base": base_key}}))
    emit({"event": "stage", "stage": "checking"})
    report, thresholds, V = build_documents_report(kb, base, dev_t, hardware(str(run)), floor)
    note_language(report, kb, base_key)
    emit({"event": "stage", "stage": "saving"})
    np.save(run / "views.npy", V.astype(np.float32))
    write_kb(kb.entries, run / "kb.json")
    (run / "thresholds.json").write_text(json.dumps(jsonable(thresholds)))
    (run / "thresholds.auto.json").write_text(json.dumps(jsonable(thresholds)))  # what was calculated, to go back to
    (run / "selfcheck.json").write_text(json.dumps({"dev": [], "test": []}))
    report["training"]["train_seconds"] = time.time() - t0
    (run / "report.json").write_text(json.dumps(jsonable(report)))
    (run / "report.md").write_text(render_markdown(report))
    emit({"event": "stage", "stage": "finished"})
    return report
