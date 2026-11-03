"""Swapping a new version of a package in, and back out. Standard library only: the package's own ci/improve.py uses it too.

A version is the set of parts that change when the model is retrained. The old ones are kept in versions/<label>/ so a bad
version can be rolled back, and a running server (serve.py) notices the swap on its next request: no restart.
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

PARTS = ("model", "kb", "thresholds.json", "eval", "README.md", "source")  # what a retraining replaces: the model and the files it was trained from
KEEP = 5  # old versions kept for rollback


def stamp() -> str:
    """A label that sorts by time and does not repeat within a second."""
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def saved(package: str | Path) -> list[Path]:
    """The versions that were live and can be rolled back to, oldest first. Half-built candidates (a dot) and staged ones, which were
    never live, are not among them."""
    root = Path(package) / "versions"
    return sorted(d for d in root.glob("*") if d.is_dir() and not d.name.startswith((".", "staged-")) and (d / "kb").is_dir()) if root.is_dir() else []


def promote(package: str | Path, candidate: str | Path, label: str | None = None) -> str:
    """Make `candidate` (a folder holding the parts of a new version) the live version. The live one is saved as versions/<label>.

    Either every part is swapped or none is. Returns the label the previous version was saved under."""
    pkg, cand = Path(package), Path(candidate)
    if not (cand / "kb").is_dir() or not (cand / "model").is_dir():
        raise ValueError(f"{cand} is not a version: it needs model/ and kb/")
    label = label or stamp()
    keep = pkg / "versions" / label
    keep.mkdir(parents=True)
    done: list[tuple[Path, Path]] = []

    def move(a: Path, b: Path) -> None:
        os.replace(a, b)
        done.append((a, b))

    try:
        for part in PARTS:
            if (pkg / part).exists():
                move(pkg / part, keep / part)
            if (cand / part).exists():
                move(cand / part, pkg / part)
    except BaseException:
        for a, b in reversed(done):  # undo, so a half-swapped package is never left behind
            os.replace(b, a)
        shutil.rmtree(keep, ignore_errors=True)
        raise
    for old in saved(pkg)[:-KEEP]:
        shutil.rmtree(old, ignore_errors=True)
    return label


def rollback(package: str | Path) -> str:
    """Go back to the newest saved version. The version being left is saved too (as rolled-back-<time>), so rolling back twice undoes it."""
    older = saved(package)
    if not older:
        raise ValueError("there is no earlier version to go back to")
    target = older[-1]
    label = "rolled-back-" + stamp()
    promote(package, target, label)
    shutil.rmtree(target, ignore_errors=True)  # its parts are live now; what is left is an empty shell
    return target.name
