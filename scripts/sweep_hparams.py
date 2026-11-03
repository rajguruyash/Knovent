"""Choose learning rate and augmentation level on DEV questions only, with seeds the final benchmark does not use.

    python scripts/sweep_hparams.py            # prints a table; the winner becomes the TrainConfig default
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from faqengine import bench  # noqa: E402
from faqengine.encoder import Encoder, pick_device  # noqa: E402
from faqengine.train import TrainConfig  # noqa: E402

SEEDS, KS, N_INTENTS = (10,), (4,), 30  # kept small: this Mac sustains about a fifth of its peak GPU speed
LRS, AUGS = (2e-5, 5e-5, 1e-4), (3,)


def main() -> None:
    data = bench.load_banking77("data")
    base = Encoder.load("minilm-l6").to(pick_device())
    out = Path("runs/sweep.jsonl")
    out.parent.mkdir(exist_ok=True)
    out.write_text("")
    rows = []

    def run(lr: float, n_aug: int) -> None:
        for k in KS:
            for seed in SEEDS:
                cfg = TrainConfig(lr=lr, n_aug=n_aug)
                r = bench.run_scenario(data, seed, N_INTENTS, k, cfg, "label", base)
                gain = r["training"]["dev_after"]["mrr@10"] - r["training"]["dev_before"]["mrr@10"]
                rows.append({"lr": lr, "n_aug": n_aug, "k": k, "seed": seed, "dev_gain_mrr": gain,
                             "test_gain_mrr": r["systems"]["tuned"]["mrr@10"] - r["systems"]["base"]["mrr@10"],
                             "steps": r["training"]["steps"], "seconds": r["training"]["train_seconds"]})
                with out.open("a") as f:
                    f.write(json.dumps(rows[-1]) + "\n")
                print(f"lr={lr:g} aug={n_aug} k={k} seed={seed}: dev gain {gain:+.4f}, steps {r['training']['steps']}", flush=True)

    for lr in LRS:
        run(lr, 3)
    best_lr = max(LRS, key=lambda lr: np.mean([r["dev_gain_mrr"] for r in rows if r["lr"] == lr and r["n_aug"] == 3]))
    for aug in AUGS:
        if aug != 3:
            run(best_lr, aug)
    print("\nselected on dev only:")
    for lr in LRS:
        sel = [r["dev_gain_mrr"] for r in rows if r["lr"] == lr and r["n_aug"] == 3]
        print(f"  lr={lr:g} (aug 3): mean dev MRR gain {np.mean(sel):+.4f}")
    for aug in AUGS:
        sel = [r["dev_gain_mrr"] for r in rows if r["lr"] == best_lr and r["n_aug"] == aug]
        print(f"  lr={best_lr:g} aug={aug}: mean dev MRR gain {np.mean(sel):+.4f}")
    print("test gains are in runs/sweep.jsonl for transparency; they were not used to choose.")


if __name__ == "__main__":
    main()
