"""Export a trained model as a self-contained package: ONNX weights, tokenizer, embeddings, thresholds, runtime.

The package is checked, not assumed: the ONNX graph must agree with the PyTorch model, the int8 version is
only shipped if it keeps retrieval quality, and the thresholds are recalibrated on the engine that is actually
shipped, so an exported engine behaves the way the evaluation report says it does.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import numpy as np
import torch

from . import metrics
from .encoder import Encoder
from .evaluate import calibrate
from .kb import KB, Queries

RUNTIME = Path(__file__).parent / "runtime"
CI = Path(__file__).parent / "ci"
INT8_MAX_DEV_DROP = 0.005  # int8 is shipped only if dev Hit@1 falls by no more than this
INT8_MIN_COSINE = 0.98


class _Graph(torch.nn.Module):
    def __init__(self, enc: Encoder):
        super().__init__()
        self.enc = enc

    def forward(self, input_ids: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
        return self.enc(input_ids, attention_mask)


def _matches_torch(path: Path, graph: _Graph) -> bool:
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    for batch, seq in ((1, 7), (3, 40)):  # different shapes, so the dynamic axes are exercised
        ids = torch.randint(1000, 2000, (batch, seq))
        mask = torch.ones_like(ids)
        mask[-1, seq // 2:] = 0
        with torch.no_grad():
            want = graph(ids, mask).numpy()
        got = sess.run(None, {"input_ids": ids.numpy(), "attention_mask": mask.numpy()})[0]
        if got.shape != want.shape or np.abs(got - want).max() > 1e-3:
            return False
    return True


SECRET_PATTERNS = re.compile(r"sk-[A-Za-z0-9_-]{16,}|sk-ant-[A-Za-z0-9_-]{16,}|AIza[0-9A-Za-z_-]{30,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.|\"api_key\"\s*:\s*\"[^\"]{6,}|service_role", re.I)
SECRET_FREE_DIRS = ("runtime", "ci", "model", "eval")  # code, settings and measurements: no business text, so a key-shaped string here is a key


def assert_no_secrets(root: Path) -> None:
    """A package must never carry a credential: an API key, a token, a service-role key, an llm.json. Language-model settings are not read by the exporter at all; this is the second lock."""
    for rel in SECRET_FREE_DIRS:
        for f in (root / rel).rglob("*") if (root / rel).is_dir() else []:
            if f.is_file() and f.suffix in (".py", ".json", ".txt", ".md", ".csv", ".env", ".yaml", ".yml", ".cfg", ".toml", ".ini", ""):
                if SECRET_PATTERNS.search(f.read_text(errors="ignore")):
                    raise RuntimeError(f"refusing to export: {f.relative_to(root)} looks like it contains a credential")
    if any(f.name in ("llm.json", ".env") or f.name.startswith(".llm-") for f in root.rglob("*") if f.is_file()):
        raise RuntimeError("refusing to export: a language-model settings or .env file is in the package folder")


def to_onnx(enc: Encoder, path: Path) -> str:
    """Write the fp32 ONNX graph; returns which exporter produced a graph that matches PyTorch."""
    enc = enc.to("cpu").eval()
    graph = _Graph(enc).eval()
    ids = torch.randint(1000, 2000, (2, 12))
    mask = torch.ones_like(ids)
    batch, seq = torch.export.Dim("batch", min=1, max=1024), torch.export.Dim("seq", min=2, max=512)
    attempts = {
        "dynamo": dict(dynamo=True, external_data=False, dynamic_shapes={"input_ids": {0: batch, 1: seq}, "attention_mask": {0: batch, 1: seq}}),
        "torchscript": dict(dynamo=False, opset_version=17, dynamic_axes={
            "input_ids": {0: "batch", 1: "seq"}, "attention_mask": {0: "batch", 1: "seq"}, "sentence_embedding": {0: "batch"}}),
    }
    errors = []
    for name, kw in attempts.items():
        try:
            torch.onnx.export(graph, (ids, mask), str(path), input_names=["input_ids", "attention_mask"], output_names=["sentence_embedding"], **kw)
            if _matches_torch(path, graph):
                return name
            errors.append(f"{name}: graph does not match PyTorch")
        except Exception as exc:  # exporters fail in many ways; try the next one
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
    raise RuntimeError("ONNX export failed: " + " | ".join(errors))


def quantize(src: Path, dst: Path) -> None:
    from onnxruntime.quantization import QuantType, quantize_dynamic

    quantize_dynamic(str(src), str(dst), weight_type=QuantType.QInt8, per_channel=True)  # per-channel keeps cosine ~0.99; per-tensor lost ~4%


def _mb(path: Path) -> float:
    return path.stat().st_size / 2**20


def _engine(root: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("faqengine_runtime_engine", root / "runtime" / "engine.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # the very file that ships in the package
    return mod.Engine(root)


def _set_model(root: Path, cfg: dict, onnx: str) -> None:
    (root / "model" / "engine.json").write_text(json.dumps({**cfg, "onnx": onnx}, indent=2))


README = """# {name}

A self-contained FAQ retrieval engine: {entries} entries, {lineage} `{hf_id}` (licence: {license}).
It runs on CPU with numpy, onnxruntime and tokenizers only. It never calls the platform that trained it.
{tuning_note}

## Run it

```bash
pip install -r runtime/requirements.txt
python runtime/engine.py . "your question here"          # one question, JSON out
python runtime/serve.py --package . --port 8080         # HTTP API
```

```bash
curl -s localhost:8080/answer   -d '{{"query": "your question here"}}'
curl -s localhost:8080/retrieve -d '{{"query": "your question here", "k": 5}}'
```

Set `FAQENGINE_API_KEY` to require `Authorization: Bearer <key>`. Use `--host 0.0.0.0` to listen on all
interfaces, and `--cors` if a web page on another origin will call it.

## Use it from your own agent

```python
import sys
sys.path.insert(0, "runtime")
from engine import Engine

engine = Engine(".")
r = engine.answer(user_question)
if r["decision"] == "direct":      # confident: return the stored answer, no LLM call
    reply = r["answer"]
elif r["decision"] == "llm":       # related but not certain: let your LLM answer from these entries
    reply = your_llm(user_question, context=r["context"])
else:                              # not in the knowledge base: hand off or say you don't know
    reply = "I don't have that information."
```

`r["hits"]` lists the closest entries with their scores, `r["confidence"]` is the best score.

## What is inside

| Path | What it is |
|---|---|
| `model/{onnx}` | the embedding model ({model_mb:.1f} MB) |
| `model/tokenizer.json`, `model/engine.json` | tokenizer and settings (pooling, prefixes, length) |
| `kb/entries.json`, `kb/embeddings.npy` | your FAQ entries and their precomputed vectors |
| `thresholds.json` | the two score thresholds that choose direct / llm / none |
| `eval/report.md` | how well it performed on questions it had not seen |
| `runtime/` | the engine and the HTTP server |
| `source/` | the files this engine was made from, and the deployment regression suite if you gave one: what retraining starts from and is checked against. (Your held-out benchmark is kept for reporting and never used by `ci/improve.py`.) |
| `ci/` | `improve.py` and a GitHub Actions workflow: keep it improving in production (below) |

{thresholds_text} They were chosen on held-out questions; the report shows what they achieve.
If you change the FAQ, retrain: the embeddings and thresholds belong to this model.

## Keep it improving in production

Teach it when it gets something wrong, and it searches the new question from the next request on, with no retraining and no restart:

```bash
curl -s localhost:8080/feedback -d '{{"question": "can I post it back", "entry_id": "returns", "verdict": "approve"}}'
python ci/improve.py add returns "can I post it back"      # the same, from a terminal
python ci/improve.py retrain                              # rebuild from source/ + what it learned; promoted only if not worse on your held-out questions
python ci/improve.py rollback                             # go back to the previous version
```

Teaching needs a key when the server is open to the network (`FAQENGINE_FEEDBACK_KEY`). `ci/README.md` explains the loop, the promotion rule and the
GitHub Actions workflow that runs it on a schedule.

Base model: `{hf_id}`, licence {license}. The weights here are derived from it; keep this notice.
"""


def _verify(enc, spec, kb: KB, eng, onnx: Path, dev) -> dict:
    """Evidence that the package holds the model it was made from, and what that model is: how far its weights are from the pretrained ones (zero means the
    pretrained model ships), the checksum of the file, and how often the package retrieves the same top entry as the PyTorch model it was exported from."""
    import hashlib

    from .train import weight_change

    state = lambda m: {k: v.detach().cpu() for k, v in m.state_dict().items()}  # noqa: E731
    out: dict = {"onnx": onnx.name, "onnx_sha256": hashlib.sha256(onnx.read_bytes()).hexdigest()}
    try:
        out["weights_vs_pretrained"] = weight_change(state(Encoder.load(spec.key)), state(enc))
        out["fine_tuned"] = out["weights_vs_pretrained"]["relative_l2"] > 0
    except Exception as e:  # noqa: BLE001 - the pretrained model may not be available offline; say so instead of failing the export
        out["weights_vs_pretrained"], out["fine_tuned"] = None, None
        out["note"] = f"the pretrained model could not be loaded to compare weights ({type(e).__name__})"
    cpu = torch.device("cpu")
    queries = ([e.questions[0] for e in kb.entries] + (list(dev.texts[:64]) if dev is not None else []))[:120]
    torch_scores = kb.entry_max(enc.encode(queries, "query", device=cpu) @ enc.encode([v.text for v in kb.views], "doc", device=cpu).T)
    package_scores = eng.scores(queries)
    out["queries_compared"] = len(queries)
    out["top1_agreement_with_pytorch"] = float((torch_scores.argmax(1) == package_scores.argmax(1)).mean())
    return out


def export_package(
    run_dir: str | Path,
    kb: KB,
    out_dir: str | Path,
    dev: Queries | None = None,
    bench_queries: list[str] | None = None,
    report: dict | None = None,
    name: str = "faq-engine",
    keep_fp32: bool = False,
    target_precision: float = 0.95,
    thresholds_override: dict | None = None,
    extra_files: dict[str, str] | None = None,
    sources: list[str | Path] | None = None,
    heldout: str | Path | None = None,
    web: list[dict] | None = None,
    regression: str | Path | None = None,
) -> dict:
    """`sources` (the files the knowledge base was read from) and `heldout` are copied into the package, so that ci/improve.py can retrain it later.
    `web` ([{file, url}]: which of the sources are web pages, and where from) lets `retrain --refresh-web` read them again."""
    run, out = Path(run_dir), Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    (out / "model").mkdir(parents=True)
    (out / "kb").mkdir()
    (out / "eval").mkdir()
    shutil.copytree(RUNTIME, out / "runtime", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(CI, out / "ci", ignore=shutil.ignore_patterns("__pycache__"))
    for src in sources or []:
        (out / "source" / "files").mkdir(parents=True, exist_ok=True)
        shutil.copy(src, out / "source" / "files" / Path(src).name)
    if heldout:
        (out / "source").mkdir(exist_ok=True)
        shutil.copy(heldout, out / "source" / "heldout.csv")
    if regression:  # the deployment regression suite: what ci/improve.py checks a new version against before it replaces the live one
        (out / "source").mkdir(exist_ok=True)
        shutil.copy(regression, out / "source" / "regression.csv")
    if web:
        (out / "source").mkdir(exist_ok=True)
        (out / "source" / "web.json").write_text(json.dumps(web, indent=1))

    enc = Encoder.load(run / "model", attn="eager")
    spec = enc.spec
    fp32, int8 = out / "model" / "encoder.fp32.onnx", out / "model" / "encoder.int8.onnx"
    exporter = to_onnx(enc, fp32)
    quantize(fp32, int8)
    shutil.copy(run / "model" / "tokenizer.json", out / "model" / "tokenizer.json")
    tok = enc.tokenizer
    cfg = {
        "onnx": fp32.name, "max_len": spec.max_len, "pad_id": int(tok.pad_token_id), "pad_token": tok.pad_token,
        "query_prefix": spec.query_prefix, "doc_prefix": spec.doc_prefix, "pooling": spec.pooling, "dim": enc.dim,
        "base": spec.key, "hf_id": spec.hf_id, "license": spec.license,
    }
    (out / "kb" / "entries.json").write_text(json.dumps({
        "entries": [{"id": e.id, "questions": e.questions, "answer": e.answer, "category": e.category, "kind": e.kind, "source": e.source} for e in kb.entries],
        "views": [{"text": v.text, "entry": v.entry, "kind": v.kind} for v in kb.views]}, ensure_ascii=False, indent=1))
    (out / "thresholds.json").write_text(json.dumps({"direct": None, "floor": 0.0}))

    # compare the two graphs on dev, then pick
    sample = ([e.questions[0] for e in kb.entries] + (dev.texts[:64] if dev else []))[:96]
    want = enc.encode(sample, "query", device=torch.device("cpu"))
    result: dict = {"exporter": exporter, "variants": {}}
    for label, path in (("fp32", fp32), ("int8", int8)):
        _set_model(out, cfg, path.name)
        eng = _engine(out)
        cos = (eng.embed(sample) * want).sum(1)
        row = {"model_mb": _mb(path), "parity_cosine_mean": float(cos.mean()), "parity_cosine_min": float(cos.min())}
        if dev is not None and len(dev):
            row["dev_hit@1"] = float(metrics.summarize(eng.scores(dev.texts), dev.gold)["hit@1"])
        result["variants"][label] = row
    f, q = result["variants"]["fp32"], result["variants"]["int8"]
    use_int8 = q["parity_cosine_mean"] >= INT8_MIN_COSINE and ("dev_hit@1" not in q or q["dev_hit@1"] >= f["dev_hit@1"] - INT8_MAX_DEV_DROP)
    chosen = int8 if use_int8 else fp32
    result.update({"onnx": chosen.name, "int8_chosen": use_int8})
    if not keep_fp32:
        (fp32 if use_int8 else int8).unlink()
    _set_model(out, cfg, chosen.name)

    # thresholds and embeddings come from the engine that ships
    eng = _engine(out)
    if thresholds_override is not None:  # decided elsewhere (for example from a person's approvals), so not recalibrated here
        th = {"direct_reason": None, **thresholds_override}
    else:
        th = calibrate(eng.scores(dev.texts), dev.gold, target_precision) if dev is not None and len(dev) else {"direct": None, "floor": 0.0}
    (out / "thresholds.json").write_text(json.dumps(th, indent=2))
    eng.thresholds = th
    np.save(out / "kb" / "embeddings.npy", eng.V)
    result["verification"] = _verify(enc, spec, kb, eng, chosen, dev)
    (out / "eval" / "verification.json").write_text(json.dumps(result["verification"], indent=1))

    # latency of the whole query path on CPU, one question at a time
    queries = (bench_queries or [e.questions[0] for e in kb.entries])[:200]
    for q_text in queries[:5]:
        eng.retrieve(q_text)
    times = []
    for q_text in queries:
        t0 = time.perf_counter()
        eng.retrieve(q_text)
        times.append((time.perf_counter() - t0) * 1000)

    if report is not None:
        runtime = {"onnx": chosen.name, "model_mb": _mb(chosen), "latency_ms_p50": float(np.percentile(times, 50)),
                   "latency_ms_p95": float(np.percentile(times, 95)), "parity_cosine_mean": result["variants"]["int8" if use_int8 else "fp32"]["parity_cosine_mean"]}
        from .evaluate import save_report

        report = {**report, "runtime": {**runtime, "package_mb": 0.0}}
        save_report(report, out / "eval")
    for rel, text in (extra_files or {}).items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    train_log = json.loads((run / "train_log.json").read_text()) if (run / "train_log.json").exists() else {}
    tuned = train_log.get("beat_untuned_on_dev", True)
    if th["direct"] is None:
        why = th.get("direct_reason") or "no dev questions were given"
        thresholds_text = (f"There is no direct-answer threshold ({why}). Send everything with a score of {th['floor']:.3f} or more "
                           "to your LLM with the matched entries as context, and treat anything lower as not in the knowledge base.")
    else:
        thresholds_text = (f"Answer directly at a score of {th['direct']:.3f} or more; between {th['floor']:.3f} and that, give the matched "
                           f"entries to your LLM; below {th['floor']:.3f}, the knowledge base does not have it.")
    (out / "README.md").write_text(README.format(
        name=name, entries=len(kb), hf_id=spec.hf_id, license=spec.license, onnx=chosen.name, model_mb=_mb(chosen),
        lineage="fine-tuned from" if tuned else "built on",
        tuning_note="" if tuned else "\nFine-tuning did not beat the untuned model on held-out questions, so the model weights are unchanged.\n",
        thresholds_text=thresholds_text))
    assert_no_secrets(out)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 2**20
    if report is not None:  # now the package size is known
        report["runtime"]["package_mb"] = size
        save_report(report, out / "eval")
        size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file()) / 2**20
    archive = shutil.make_archive(str(out), "zip", root_dir=out.parent, base_dir=out.name)
    result.update({
        "package_mb": size, "zip": archive, "zip_mb": Path(archive).stat().st_size / 2**20, "thresholds": th,
        "latency_ms_p50": float(np.percentile(times, 50)), "latency_ms_p95": float(np.percentile(times, 95)), "queries_timed": len(times),
    })
    return result
