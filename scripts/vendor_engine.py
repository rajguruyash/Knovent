"""Copies what the hosted engine imports (and its embedding model) into web/engine/, so the Vercel project (whose root is web/) can run the engine as a Python function without a second host.

    python scripts/vendor_engine.py           write web/engine/ from src/ and hosted_models/
    python scripts/vendor_engine.py --check   exit 1 if web/engine/ differs from src/ (a test runs this: the copy is never edited by hand)

The list of files is not written here: it is every faqengine module that `faqengine.hosted.service` really loads, found by importing it in a fresh interpreter (the Dockerfile's hand-written list
once missed `inference/`). The model is the shared MiniLM-L6 int8 ONNX file built by scripts/build_base_model.py (Apache-2.0, sentence-transformers/all-MiniLM-L6-v2)."""
from __future__ import annotations

import filecmp
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC, OUT = ROOT / "src", ROOT / "web" / "engine"
MODEL_FROM, MODEL_TO = ROOT / "hosted_models" / "minilm-l6" / "model", OUT / "models" / "minilm-l6" / "model"
MODEL_FILES = ("encoder.int8.onnx", "engine.json", "tokenizer.json")


def needed() -> list[str]:
    code = ("import sys, os; import faqengine.hosted.service; "
            "print(json.dumps(sorted({os.path.relpath(m.__file__, 'src').replace(os.sep, '/') for n, m in sys.modules.items() if n.split('.')[0] == 'faqengine' and getattr(m, '__file__', None)})))")
    out = subprocess.run([sys.executable, "-c", "import json; " + code], cwd=ROOT, env={"PYTHONPATH": "src", "PATH": ""}, capture_output=True, text=True)
    if out.returncode:
        sys.exit("could not import faqengine.hosted.service:\n" + out.stderr[-800:])
    return json.loads(out.stdout.strip().splitlines()[-1])


def wanted() -> dict[Path, Path]:
    pairs = {OUT / rel: SRC / rel for rel in needed()}
    pairs.update({MODEL_TO / n: MODEL_FROM / n for n in MODEL_FILES})
    return pairs


def check() -> list[str]:
    problems, pairs = [], wanted()
    for dest, src in pairs.items():
        if not src.exists():
            problems.append(f"missing source {src.relative_to(ROOT)}")
        elif not dest.exists():
            problems.append(f"not copied: {dest.relative_to(ROOT)}")
        elif not filecmp.cmp(src, dest, shallow=False):
            problems.append(f"out of date: {dest.relative_to(ROOT)}")
    if OUT.exists():
        for f in OUT.rglob("*"):
            if f.is_file() and "__pycache__" not in f.parts and f not in pairs:
                problems.append(f"left over: {f.relative_to(ROOT)}")
    return problems


def main() -> int:
    if "--check" in sys.argv:
        problems = check()
        print("\n".join(problems) if problems else "web/engine matches src")
        return 1 if problems else 0
    shutil.rmtree(OUT, ignore_errors=True)
    for dest, src in wanted().items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    print(f"copied {len(wanted())} files into {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
