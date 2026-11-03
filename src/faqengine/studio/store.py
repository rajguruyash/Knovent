"""Projects on disk: one folder each, so a restart or a crash loses nothing.

    projects/<id>/  meta.json  faq.csv|json|jsonl  status.json  events.jsonl  export.json  worker.log
                    run/ (model, kb.json, views.npy, report.json, thresholds.json, ...)
                    tests.jsonl  feedback.jsonl  package/
Training and exporting run in a separate worker process (worker.py) that reports through status.json and events.jsonl,
so a heavy job cannot freeze the page and its memory is returned when it ends.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import hashlib

from ..heldout import load_heldout
from ..kb import KB, load_kb
from ..sources import SUPPORTED, TABLE_SUFFIXES, _table_rows, describe_table, kind_of, remap_table, test_question_columns
from . import feedback as fb

ID = re.compile(r"^[0-9a-f]{12}$")
MAX_FILE = 15 * 1024 * 1024  # one file
MAX_TOTAL = 25 * 1024 * 1024  # all the files of a project
MAX_UPLOAD = 36 * 1024 * 1024  # one request (files travel base64-encoded)
MAX_ENTRIES = 1000  # FAQ question/answer entries
MAX_PASSAGES = 4000  # pieces of documents
DEFAULT_BASE = "minilm-l6"
SUFFIXES = SUPPORTED
RUNNING = ("training",)


class Busy(RuntimeError):
    pass


def write_json(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj))
    tmp.replace(path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def read_lines(path: Path) -> list[dict]:
    out = []
    try:
        for line in path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass  # a half-written last line while the worker is mid-write
    except OSError:
        pass
    return out


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "projects").mkdir(parents=True, exist_ok=True)
        self.procs: dict[str, subprocess.Popen] = {}
        self.lock = threading.RLock()

    # ---- projects
    def dir(self, pid: str) -> Path:
        d = self.root / "projects" / (pid if ID.match(pid or "") else "invalid")
        if not d.is_dir():
            raise KeyError(pid)
        return d

    def create(self, name: str, filename: str, text: str) -> str:
        return self.create_many(name, [(filename, text.lstrip("\ufeff").encode("utf-8"))])

    def create_many(self, name: str, files: list[tuple[str, bytes]], extra: dict[str, dict] | None = None) -> str:
        if not files:
            raise ValueError("Add at least one file.")
        pid = uuid.uuid4().hex[:12]
        d = self.root / "projects" / pid
        (d / "sources").mkdir(parents=True)
        try:
            records = self._write_sources(d, files, [], extra)
            self._validate(d, records)
        except Exception:
            shutil.rmtree(d, ignore_errors=True)
            raise
        first = files[0][0]
        write_json(d / "meta.json", {"id": pid, "name": (name or Path(first).stem)[:80], "filename": first, "suffix": Path(first).suffix.lower(),
                                     "sources": records, "created": time.time(), "target_precision": 0.95})
        write_json(d / "status.json", {"state": "new"})
        return pid

    # ---- the files a project is made of
    def sources(self, pid: str) -> list[dict]:
        d, m = self.dir(pid), self.meta(pid)
        if "sources" in m:
            return m["sources"]
        legacy = d / f"faq{m['suffix']}"  # a project made when only one FAQ file was allowed
        return [{"name": m["filename"], "file": legacy.name, "kind": "faq", "size": legacy.stat().st_size, "added": m["created"]}]

    def source_paths(self, pid: str) -> list[Path]:
        d = self.dir(pid)
        return [d / r["file"] for r in self.sources(pid)]

    def kb(self, pid: str) -> KB:
        return load_kb(self.source_paths(pid))

    @staticmethod
    def _safe(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "file").name).lstrip(".")[:80] or "file"

    def _write_sources(self, d: Path, files: list[tuple[str, bytes]], existing: list[dict], extra: dict[str, dict] | None = None) -> list[dict]:
        """Write files into sources/ (a file with the same name replaces the old one); returns the new records. Nothing is validated here.
        `extra` adds fields to a file's record by its name (a web page records the address it was read from)."""
        total = sum(r["size"] for r in existing if r["name"] not in {n for n, _ in files}) + sum(len(b) for _, b in files)
        if total > MAX_TOTAL:
            raise ValueError(f"The files add up to more than {MAX_TOTAL // 2**20} MB.")
        records = [r for r in existing if r["name"] not in {n for n, _ in files}]
        for name, data in files:
            suffix = Path(name or "").suffix.lower()
            if suffix not in SUFFIXES:
                raise ValueError(f"{name}: unsupported file type. Use {', '.join(SUFFIXES)}.")
            if len(data) > MAX_FILE:
                raise ValueError(f"{name} is larger than {MAX_FILE // 2**20} MB.")
            if not data.strip():
                raise ValueError(f"{name} is empty.")
            rel = f"sources/{self._safe(name)}"
            (d / rel).write_bytes(data)
            records.append({"name": name, "file": rel, "kind": None, "size": len(data), "added": time.time(), **(extra or {}).get(name, {})})
        return records

    def _validate(self, d: Path, records: list[dict]) -> KB:
        """Every file must read on its own, and together they must make one knowledge base within the limits. Sets each record's kind."""
        for r in records:
            path = d / r["file"]
            try:
                load_kb(path)
                r["kind"] = {"records": "records"}.get((r.get("mapping") or {}).get("as"), kind_of(path))  # a table the person chose to read as records stays labelled so
            except json.JSONDecodeError as e:
                raise ValueError(f"{r['name']} is not valid {path.suffix[1:].upper()}: {e.msg} (line {e.lineno}).") from None
            except ValueError as e:
                msg = str(e)
                raise ValueError(msg if r["name"] in msg or path.name in msg else f"{r['name']}: {msg}") from None
            except Exception as e:
                raise ValueError(f"Could not read {r['name']} ({type(e).__name__}). Use UTF-8 CSV, JSON or JSONL with a question column, or a text, Markdown, HTML, PDF or Word document.") from None
        kb = load_kb([d / r["file"] for r in records])
        faq, _ = kb.faq_only()
        n_faq = len(faq) if faq else 0
        if n_faq > MAX_ENTRIES:
            raise ValueError(f"This version handles up to {MAX_ENTRIES} FAQ entries; these files have {n_faq}.")
        if len(kb) - n_faq > MAX_PASSAGES:
            raise ValueError(f"This version handles up to {MAX_PASSAGES} document passages; these files make {len(kb) - n_faq}.")
        return kb

    def _migrate(self, pid: str) -> None:
        """Give a project made before several files were allowed the new layout (the original file is kept where it was)."""
        m = self.meta(pid)
        if "sources" in m:
            return
        d = self.dir(pid)
        rec = self.sources(pid)[0]
        rel = f"sources/{self._safe(rec['name'])}"
        (d / "sources").mkdir(exist_ok=True)
        shutil.copy2(d / rec["file"], d / rel)
        self.set_meta(pid, sources=[{**rec, "file": rel}])

    @staticmethod
    def _units(path: Path) -> set[tuple[str, str]]:
        """What a file says, as (heading or question, text) pairs: enough to tell what changed between two readings of it."""
        try:
            return {(e.question, e.answer) for e in load_kb(path).entries}
        except Exception:  # noqa: BLE001 - a file that cannot be read has no units to compare; the version note is a convenience
            return set()

    def _versioned(self, d: Path, records: list[dict], old: list[dict], before: dict[str, tuple[set, str]], added: set[str]) -> None:
        """Gives every file just added or replaced its version and what changed since the last reading: counts of entries and passages gained, lost and kept. Files not named in this
        upload are left as they were; the same bytes again are not a new version."""
        previous = {r["name"]: r for r in old}
        for r in records:
            if r["name"] not in added:
                continue
            path = d / r["file"]
            now, sha = self._units(path), hashlib.sha1(path.read_bytes()).hexdigest()
            prior = previous.get(r["name"])
            if prior is not None and r["name"] in before and before[r["name"]][1] == sha:
                r["version"], r["sha1"], r["history"] = prior.get("version", 1), sha, prior.get("history", [])
                continue
            was = before[r["name"]][0] if r["name"] in before else None
            note = {"version": (prior.get("version", 1) + 1) if prior is not None else 1, "at": r.get("added"), "size": r["size"], "sha1": sha, "units": len(now)}
            if was is not None:
                note.update(added=len(now - was), removed=len(was - now), unchanged=len(now & was))
            r["version"], r["sha1"] = note["version"], sha
            r["history"] = ((prior or {}).get("history") or [])[-19:] + [note]

    def add_sources(self, pid: str, files: list[tuple[str, bytes]], extra: dict[str, dict] | None = None) -> None:
        with self.lock:
            d = self.dir(pid)
            self._migrate(pid)
            old = self.sources(pid)
            names = {n for n, _ in files}
            replaced = [r for r in old if r["name"] in names]
            backup = {r["file"]: (d / r["file"]).read_bytes() for r in replaced}
            before = {r["name"]: (self._units(d / r["file"]), hashlib.sha1((d / r["file"]).read_bytes()).hexdigest()) for r in replaced}
            try:
                records = self._write_sources(d, files, old, extra)
                self._validate(d, records)
                self._versioned(d, records, old, before, names)
            except Exception:
                for rel, data in backup.items():  # put back what a failed replacement overwrote
                    (d / rel).write_bytes(data)
                for name, _ in files:
                    if name not in {r["name"] for r in replaced}:
                        (d / f"sources/{self._safe(name)}").unlink(missing_ok=True)
                raise
            self.set_meta(pid, sources=records)

    def mark_indexed(self, pid: str) -> None:
        """The search index was brought up to date with the current files without training: the banner says the model itself still has to learn them."""
        self.set_meta(pid, indexed_signature=self.signature(pid))

    def remove_source(self, pid: str, name: str) -> None:
        with self.lock:
            d = self.dir(pid)
            self._migrate(pid)
            old = self.sources(pid)
            keep = [r for r in old if r["name"] != name]
            if len(keep) == len(old):
                raise KeyError(name)
            if not keep:
                raise ValueError("A project needs at least one file. Delete the project instead.")
            self._validate(d, keep)
            self.set_meta(pid, sources=keep)
            gone = next(r for r in old if r["name"] == name)
            (d / gone["file"]).unlink(missing_ok=True)
            if gone.get("original"):
                (d / gone["original"]).unlink(missing_ok=True)

    def _record(self, pid: str, name: str) -> dict:
        return next(r for r in self.sources(pid) if r["name"] == name)  # StopIteration is turned into "not found" by the caller

    def table_info(self, pid: str, name: str) -> dict:
        """The columns of a table file, a few of its rows, how it is read now, and the choice made earlier if there was one (always from the file as it was uploaded)."""
        try:
            rec = self._record(pid, name)
        except StopIteration:
            raise KeyError(name) from None
        path = self.dir(pid) / (rec.get("original") or rec["file"])
        if path.suffix.lower() not in TABLE_SUFFIXES:
            raise ValueError(f"{name} is a document, not a table: it has no columns to choose.")
        return {**describe_table(path), "name": name, "mapping": rec.get("mapping")}

    def apply_mapping(self, pid: str, name: str, mapping: dict) -> None:
        """Read a table with the person's choice of columns. The uploaded file is kept untouched; what the project uses from now on is a copy whose headers say what each column is,
        so training, export and retraining all just find the usual names. A choice that makes the project unreadable is undone."""
        with self.lock:
            d = self.dir(pid)
            self._migrate(pid)
            old = self.sources(pid)
            try:
                rec = next(r for r in old if r["name"] == name)
            except StopIteration:
                raise KeyError(name) from None
            if Path(rec.get("original") or rec["file"]).suffix.lower() not in TABLE_SUFFIXES:
                raise ValueError(f"{name} is a document, not a table: it has no columns to choose.")
            current = d / rec["file"]
            original_rel = rec.get("original")
            if not original_rel:  # keep the file as it was uploaded, once
                (d / "sources" / "originals").mkdir(parents=True, exist_ok=True)
                original_rel = f"sources/originals/{current.name}"
                shutil.copy2(current, d / original_rel)
            data = remap_table(d / original_rel, mapping)
            new_rel = f"sources/{current.stem}.csv"
            backup = current.read_bytes() if current.exists() else None
            (d / new_rel).write_bytes(data)
            new = {**rec, "file": new_rel, "original": original_rel, "mapping": mapping, "size": len(data)}
            records = [new if r["name"] == name else r for r in old]
            try:
                self._validate(d, records)
            except Exception:
                if new_rel == rec["file"] and backup is not None:
                    (d / new_rel).write_bytes(backup)
                else:
                    (d / new_rel).unlink(missing_ok=True)
                raise
            if new_rel != rec["file"]:
                current.unlink(missing_ok=True)
            self.set_meta(pid, sources=records)

    def ensure_isolation(self, pid: str) -> list[str]:
        """Run quarantine_test_files once per project (new files are checked when they are added)."""
        if self.meta(pid).get("isolation_checked"):
            return []
        notes = self.quarantine_test_files(pid)
        self.set_meta(pid, isolation_checked=True)
        return notes

    def quarantine_test_files(self, pid: str) -> list[str]:
        """Take test questions out of the sources. A project made before test files were recognised may hold one as a "FAQ" (the test questions became
        searchable, trainable entries, so every evaluation of them measured the engine finding its own input). Each such file is removed from the sources
        and becomes the held-out set (or, if there already is one, is kept aside in quarantine/). Returns what was done, in words."""
        with self.lock:
            d = self.dir(pid)
            self._migrate(pid)
            old, flagged = self.sources(pid), []
            for r in old:
                path = d / r["file"]
                if path.suffix.lower() in TABLE_SUFFIXES and path.is_file():
                    try:
                        rows = _table_rows(path)
                        why = test_question_columns({k for x in rows[:50] for k in x}, rows)
                    except Exception:  # noqa: BLE001 - a file that cannot be read is dealt with when it is validated
                        why = []
                    if why:
                        flagged.append(r)
            keep = [r for r in old if r not in flagged]
            if not flagged or not keep:
                return []
            notes = []
            for r in flagged:
                path = d / r["file"]
                if self.heldout_path(pid) is None:
                    shutil.copy2(path, d / "heldout.csv")
                    notes.append(f"{r['name']} holds test questions, not FAQ entries. It was removed from the sources and is now the held-out test set, used only to measure the engine.")
                else:
                    (d / "quarantine").mkdir(exist_ok=True)
                    shutil.copy2(path, d / "quarantine" / path.name)
                    notes.append(f"{r['name']} holds test questions, not FAQ entries. It was removed from the sources (a copy is in the project's quarantine folder) because a held-out set already exists.")
                path.unlink(missing_ok=True)
            self.set_meta(pid, sources=keep, notices=[*self.meta(pid).get("notices", []), *notes])
            return notes

    def signature(self, pid: str) -> str:
        """Changes whenever a file (or the held-out set) is added, replaced or removed, so a trained model can tell it is out of date."""
        h = hashlib.sha1()
        for r, p in zip(self.sources(pid), self.source_paths(pid)):
            h.update(r["name"].encode() + b"\0" + hashlib.sha1(p.read_bytes()).digest())
        held = self.heldout_path(pid)
        if held:
            h.update(b"heldout\0" + hashlib.sha1(held.read_bytes()).digest())  # the report was measured on it
        if self.meta(pid).get("finetune", True) is False:
            h.update(b"finetune-off\0")
        base = self.meta(pid).get("base")
        if base and base != DEFAULT_BASE:
            h.update(b"base\0" + base.encode())  # a model trained on another base is out of date when the base changes
        return h.hexdigest()

    # ---- optional held-out questions
    def heldout_path(self, pid: str) -> Path | None:
        p = self.dir(pid) / "heldout.csv"
        return p if p.is_file() else None

    def set_heldout(self, pid: str, text: str) -> dict:
        d = self.dir(pid)
        tmp = d / "heldout.csv.new"
        tmp.write_text(text.lstrip("\ufeff"), encoding="utf-8")
        try:
            summary = self._heldout_summary(tmp, self.kb(pid))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(d / "heldout.csv")
        return summary

    def clear_heldout(self, pid: str) -> None:
        (self.dir(pid) / "heldout.csv").unlink(missing_ok=True)

    # ---- the deployment regression suite: same format, a different job. It is checked before a new version goes live, so it influences release decisions and is never a test set.
    def regression_path(self, pid: str) -> Path | None:
        p = self.dir(pid) / "regression.csv"
        return p if p.is_file() else None

    def set_regression(self, pid: str, text: str) -> dict:
        d = self.dir(pid)
        tmp = d / "regression.csv.new"
        tmp.write_text(text.lstrip("\ufeff"), encoding="utf-8")
        try:
            summary = self._heldout_summary(tmp, self.kb(pid))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(d / "regression.csv")
        return summary

    def clear_regression(self, pid: str) -> None:
        (self.dir(pid) / "regression.csv").unlink(missing_ok=True)

    def regression_summary(self, pid: str) -> dict | None:
        p = self.regression_path(pid)
        if p is None:
            return None
        try:
            return self._heldout_summary(p, self.kb(pid))
        except ValueError as e:
            return {"error": str(e)}

    @staticmethod
    def _heldout_summary(path: Path, kb: KB) -> dict:
        ho = load_heldout(path, kb)
        if not len(ho):
            raise ValueError("No usable held-out questions in that file (each needs a query, a type and, unless out of scope, an entry_id).")
        return {**ho.counts(), "removed": ho.removed[:20], "warnings": ho.warnings[:10], "exact_duplicates": ho.stats.get("exact_duplicates", [])[:10]}

    def heldout_summary(self, pid: str) -> dict | None:
        p = self.heldout_path(pid)
        if p is None:
            return None
        try:
            return self._heldout_summary(p, self.kb(pid))
        except ValueError as e:  # the files changed since: say what is wrong instead of failing the page
            return {"error": str(e)}

    def delete(self, pid: str) -> None:
        with self.lock:
            d = self.dir(pid)
            proc = self.procs.pop(pid, None)
            if proc and proc.poll() is None:
                proc.terminate()
            shutil.rmtree(d, ignore_errors=True)

    def meta(self, pid: str) -> dict:
        return read_json(self.dir(pid) / "meta.json", {})

    def set_meta(self, pid: str, **kw) -> None:
        d = self.dir(pid)
        write_json(d / "meta.json", {**self.meta(pid), **kw})

    def list(self) -> list[dict]:
        out = []
        for d in sorted((self.root / "projects").iterdir()):
            m = read_json(d / "meta.json")
            if m:
                out.append({"id": m["id"], "name": m["name"], "created": m["created"], "state": self.status(m["id"]).get("state", "new")})
        return sorted(out, key=lambda p: -p["created"])

    def faq_summary(self, pid: str) -> dict:
        kb = self.kb(pid)
        faq, _ = kb.faq_only()
        records = self.sources(pid)
        counts: dict[str, list[int]] = {}
        for e in kb.entries:
            counts.setdefault(e.source, [0, 0])[0 if e.kind == "faq" else 1] += 1
        label = lambda r: r.get("url") or Path(r["file"]).name  # noqa: E731  (a web page's passages say which address they came from)
        sources = [{"name": r["name"], "kind": r["kind"], "size": r["size"], "url": r.get("url"), "faq_entries": counts.get(label(r), [0, 0])[0],
                    "passages": counts.get(label(r), [0, 0])[1], "table": Path(r.get("original") or r["file"]).suffix.lower() in TABLE_SUFFIXES, "mapped": bool(r.get("mapping")),
                    "version": r.get("version", 1), "changed": next(({k: h[k] for k in ("added", "removed", "unchanged")} for h in reversed(r.get("history") or []) if "added" in h), None),
                    "updated": r.get("added")} for r in records]
        shown = {label(r): r.get("url") or r["name"] for r in records}
        return {"entries": len(faq) if faq else 0, "passages": len(kb) - (len(faq) if faq else 0), "phrasings": sum(len(e.questions) for e in kb.entries if e.kind == "faq"),
                "warnings": kb.warnings[:50], "sources": sources,
                "preview": [{"id": e.id, "question": e.question, "answer": e.answer, "phrasings": len(e.questions), "category": e.category, "kind": e.kind,
                             "source": shown.get(e.source, e.source)} for e in kb.entries[:300]],
                "entry_list": [{"id": e.id, "question": e.question, "kind": e.kind, "source": shown.get(e.source, e.source)} for e in kb.entries[:2500]]}

    # ---- jobs
    def _alive(self, pid: str, d: Path) -> bool:
        proc = self.procs.get(pid)
        if proc is not None:
            return proc.poll() is None
        try:
            os.kill(int((d / "job.pid").read_text()), 0)
            return True
        except (OSError, ValueError):
            return False

    def status(self, pid: str) -> dict:
        d = self.dir(pid)
        st = read_json(d / "status.json", {"state": "new"})
        if st.get("state") in RUNNING and not self._alive(pid, d):
            st = {**st, "state": "failed", "error": st.get("error") or "The training process stopped unexpectedly. Details are in worker.log inside the project folder."}
            write_json(d / "status.json", st)
        return st

    def export_status(self, pid: str) -> dict:
        d = self.dir(pid)
        ex = read_json(d / "export.json", {"state": "idle"})
        if ex.get("state") == "running" and not self._alive(pid, d):
            ex = {**ex, "state": "failed", "error": ex.get("error") or "The export process stopped unexpectedly."}
            write_json(d / "export.json", ex)
        return ex

    def busy(self) -> str | None:
        for p in self.list():
            if p["state"] in RUNNING or self.export_status(p["id"]).get("state") == "running":
                return p["id"]
        return None

    def start(self, pid: str, kind: str, use_feedback: bool = False) -> None:
        with self.lock:
            d = self.dir(pid)
            if self.busy():
                raise Busy("Another job is already running. Wait for it to finish; only one runs at a time because training needs the whole machine.")
            if kind == "train":
                write_json(d / "status.json", {"state": "training", "stage": "reading", "started": time.time(), "error": None})
                (d / "events.jsonl").write_text("")
            else:
                write_json(d / "export.json", {"state": "running", "started": time.time(), "error": None})
            cmd = [sys.executable, "-m", "faqengine.studio.worker", kind, str(d)] + (["--use-feedback"] if use_feedback else [])
            env = {**os.environ, "PYTORCH_ENABLE_MPS_FALLBACK": "1", "TOKENIZERS_PARALLELISM": "false", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
            with open(d / "worker.log", "ab") as log:
                proc = subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True, env=env)
            self.procs[pid] = proc
            (d / "job.pid").write_text(str(proc.pid))

    def progress(self, pid: str) -> dict | None:
        events = read_lines(self.dir(pid) / "events.jsonl")
        if not events:
            return None
        steps = [e for e in events if e.get("event") == "step"]
        last = steps[-1] if steps else {}
        stride = max(1, len(steps) // 150)
        return {
            "step": last.get("step", 0), "of": last.get("of", 0), "loss": last.get("loss"), "samples_per_s": last.get("samples_per_s"),
            "eta_s": last.get("eta_s"), "elapsed_s": last.get("elapsed_s"), "mem_mb": last.get("mem_mb"),
            "device": next((e.get("device") for e in events if e.get("event") == "stage" and e.get("device")), None),
            "curve": [{"step": e["step"], "loss": e["loss"]} for e in steps[::stride]],
            "evals": [{"step": e["step"], "train_loss": e["train_loss"], "dev_hit1": e["dev"]["hit@1"], "dev_mrr": e["dev"]["mrr@10"], "dev_loss": e["dev"]["loss"]}
                      for e in events if e.get("event") == "eval"],
        }

    # ---- results
    def run_dir(self, pid: str) -> Path:
        return self.dir(pid) / "run"

    def report(self, pid: str) -> dict | None:
        return read_json(self.run_dir(pid) / "report.json")

    def thresholds(self, pid: str) -> dict | None:
        return read_json(self.run_dir(pid) / "thresholds.json")

    def thresholds_auto(self, pid: str) -> dict | None:
        """The thresholds as calculated when the model was trained: the baseline that a person's judgements and settings build on."""
        return read_json(self.run_dir(pid) / "thresholds.auto.json") or self.thresholds(pid)

    def save_thresholds(self, pid: str, th: dict) -> None:
        write_json(self.run_dir(pid) / "thresholds.json", th)

    # ---- tests and judgements
    def add_test(self, pid: str, rec: dict) -> str:
        d = self.dir(pid)
        tid = "t" + uuid.uuid4().hex[:10]
        with self.lock, open(d / "tests.jsonl", "a") as f:
            f.write(json.dumps({"id": tid, "ts": time.time(), **rec}) + "\n")
        return tid

    def add_feedback(self, pid: str, test_id: str, verdict: str, comment: str, correct_entry: str | None, entry_ids: set[str]) -> None:
        d = self.dir(pid)
        if verdict not in ("approve", "reject"):
            raise ValueError("verdict must be approve or reject")
        if not any(t["id"] == test_id for t in read_lines(d / "tests.jsonl")):
            raise KeyError(test_id)
        if correct_entry not in (None, "", "none") and correct_entry not in entry_ids:
            raise ValueError("unknown entry")
        with self.lock, open(d / "feedback.jsonl", "a") as f:
            f.write(json.dumps({"ts": time.time(), "test_id": test_id, "verdict": verdict, "comment": (comment or "")[:1000],
                                "correct_entry": correct_entry or None}) + "\n")

    def history(self, pid: str) -> list[dict]:
        d = self.dir(pid)
        judged = {f["test_id"]: f for f in read_lines(d / "feedback.jsonl")}  # a later judgement replaces an earlier one
        out = []
        for t in read_lines(d / "tests.jsonl"):
            f = judged.get(t["id"], {})
            out.append({**t, "verdict": f.get("verdict"), "comment": f.get("comment", ""), "correct_entry": f.get("correct_entry"), "judged_at": f.get("ts")})
        return out[::-1]

    def feedback_csv(self, pid: str) -> str:
        names = {e["id"]: e["question"] for e in self.faq_summary(pid)["entry_list"]}
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "question", "engine_answered_entry", "score", "outcome", "verdict", "comment", "correct_entry"])
        for h in self.history(pid):
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h["ts"])), h["query"], names.get(h.get("top_entry"), h.get("top_entry")),
                        f"{h.get('top_score', 0):.3f}", h.get("band"), h.get("verdict") or "", h.get("comment") or "", names.get(h.get("correct_entry"), h.get("correct_entry") or "")])
        return buf.getvalue()

    def current_history(self, pid: str) -> list[dict]:
        """Tests run on the model that is in use now: judgements of an older model prove nothing about this one."""
        gen = self.meta(pid).get("gen", 0)
        return [h for h in self.history(pid) if h.get("gen", 0) == gen]

    def judged_state(self, pid: str) -> dict:
        history = self.history(pid)
        current = self.current_history(pid)
        return {"tally": fb.tally(current), "history": history[:200], "earlier": len(history) - len(current), "gen": self.meta(pid).get("gen", 0)}
