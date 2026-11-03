"""Builds the shared embedding model for the hosted service: all-MiniLM-L6-v2, pretrained (not fine-tuned), as a small ONNX file that runs on CPU with onnxruntime.

    python scripts/build_base_model.py --out hosted_models/minilm-l6

The hosted service answers every business with this one model: it is loaded once and shared, and nothing is trained per business there (training stays in the local Studio, where a
business can then export its own tuned model). The script checks the quantised (int8) graph against the full-precision one and ships the int8 only if they agree.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    import torch

    from faqengine.encoder import Encoder
    from faqengine.export import quantize, to_onnx
    from faqengine.runtime.embedder import Embedder

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="minilm-l6")
    ap.add_argument("--out", default=str(ROOT / "hosted_models" / "minilm-l6"))
    ap.add_argument("--min-cosine", type=float, default=0.985, help="the int8 graph must agree with the full-precision one at least this closely (mean cosine)")
    args = ap.parse_args()
    out = Path(args.out) / "model"
    shutil.rmtree(Path(args.out), ignore_errors=True)
    out.mkdir(parents=True)
    enc = Encoder.load(args.base, attn="eager")
    spec, tok = enc.spec, enc.tokenizer
    fp32, int8 = out / "encoder.fp32.onnx", out / "encoder.int8.onnx"
    exporter = to_onnx(enc, fp32)
    quantize(fp32, int8)
    enc.tokenizer.backend_tokenizer.save(str(out / "tokenizer.json"))
    cfg = {"max_len": spec.max_len, "pad_id": int(tok.pad_token_id), "pad_token": tok.pad_token, "query_prefix": spec.query_prefix, "doc_prefix": spec.doc_prefix, "pooling": spec.pooling,
           "dim": enc.dim, "base": spec.key, "hf_id": spec.hf_id, "license": spec.license, "fine_tuned": False}
    sample = ["What are your opening hours?", "How do I return an item?", "Can I cancel my booking and get a refund?", "Do you deliver to Pune?", "How long does a repair take?", "¿Aceptan tarjetas?",
              "Is a deposit required for large orders?", "My order has not arrived", "Where can I find the warranty terms?", "Do you offer student discounts?"]
    want = enc.encode(sample, "query", device=torch.device("cpu"))
    result = {"exporter": exporter}
    for name, path in (("fp32", fp32), ("int8", int8)):
        (out / "engine.json").write_text(json.dumps({**cfg, "onnx": path.name}))
        t0 = time.perf_counter()
        e = Embedder(out)
        load_s = time.perf_counter() - t0
        got = e.embed(sample, "query")
        t1 = time.perf_counter()
        for _ in range(20):
            e.embed([sample[0]], "query")
        result[name] = {"mb": round(path.stat().st_size / 2**20, 1), "load_seconds": round(load_s, 2), "cosine_to_pytorch_mean": float((got * want).sum(1).mean()), "single_question_ms": round((time.perf_counter() - t1) / 20 * 1000, 1)}
    use = "int8" if result["int8"]["cosine_to_pytorch_mean"] >= args.min_cosine else "fp32"
    keep, drop = (int8, fp32) if use == "int8" else (fp32, int8)
    drop.unlink()
    (out / "engine.json").write_text(json.dumps({**cfg, "onnx": keep.name}, indent=2))
    result["shipped"] = use
    (Path(args.out) / "BUILD.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
