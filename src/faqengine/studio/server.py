"""The local website's server (standard library only). Binds to this computer only.

    faqengine studio            then open http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import web as webpage
from .. import language
from ..encoder import BASES
from ..kb import clean
from . import feedback as fb
from ..runtime import llm as llm_runtime
from ..runtime import shadow
from .engine import ProjectEngine
from .llm_settings import LLMSettings
from .store import MAX_UPLOAD, Busy, Store
import base64
import binascii

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".csv": "text/csv; charset=utf-8"}
TEMPLATE = ("id,question,answer,category,alt_questions\n"
            "hours,What are your opening hours?,We are open 9am to 6pm Monday to Saturday.,shop,When are you open?|What time do you close?\n"
            "returns,How do I return an item?,You can return unused items within 30 days with your receipt.,orders,\n"
            "delivery,Do you deliver?,Yes. Delivery takes 1 to 2 working days and is free over 50 pounds.,orders,Can you ship to my address?\n")
PID = r"(?P<pid>[0-9a-f]{12})"


BASE_NOTES = {
    "minilm-l6": "English. Small and fast: the default.",
    "bge-small": "English. A little larger, often a little more accurate.",
    "e5-small": "English. Small.",
    "multilingual-e5-small": "More than 90 languages, including Hindi and Spanish. Larger: about 470 MB to download once.",
}


class App:
    def __init__(self, store: Store, pipeline: str | None = None):
        self.store = store
        self.pipeline = shadow.mode_from(os.environ.get("FAQENGINE_PIPELINE") if pipeline is None else pipeline)  # off | shadow | on: the same setting, and the same code, as the hosted service
        self.engines: dict[str, ProjectEngine] = {}
        self.learn_lock = threading.Lock()
        self.engine_lock = threading.Lock()
        self._hw: tuple[float, dict | None] = (0.0, None)

    def hardware(self) -> dict:
        if time.time() - self._hw[0] > 10:
            from ..hardware import hardware

            self._hw = (time.time(), hardware(str(self.store.root)))
        return self._hw[1]

    def overview(self) -> dict:
        return {"projects": self.store.list(), "hardware": self.hardware(), "busy": self.store.busy()}

    @staticmethod
    def decode_files(body: dict) -> list[tuple[str, bytes]]:
        """[{filename, text}] for text files or [{filename, base64}] for PDF and Word; the old single filename + text form still works."""
        items = body.get("files")
        if items is None:
            items = [{"filename": body.get("filename"), "text": body.get("text")}]
        out = []
        for it in items:
            if not isinstance(it, dict) or not isinstance(it.get("filename"), str):
                raise ValueError("Each file needs a filename.")
            try:
                data = base64.b64decode(it["base64"], validate=True) if it.get("base64") is not None else str(it.get("text") or "").lstrip("\ufeff").encode("utf-8")
            except (binascii.Error, ValueError, TypeError):
                raise ValueError(f"{it['filename']}: the file contents could not be read.") from None
            out.append((it["filename"], data))
        return out

    @staticmethod
    def read_web(spec: object) -> tuple[list[tuple[str, bytes]], dict, list[str], str]:
        """Read a web page (and, if asked, the pages it links to on the same site): (files to keep, their records' extra fields, notes, site name)."""
        if not isinstance(spec, dict):
            raise ValueError("Give the address of a web page.")
        pages, notes = webpage.read_site(str(spec.get("url") or ""), spec.get("pages", 1))
        files, extra = [], {}
        for pg in pages:
            name = webpage.page_filename(pg.url)
            files.append((name, webpage.page_html(pg)))
            extra[name] = {"url": pg.url, "title": pg.title, "fetched": time.time()}
        return files, extra, notes, urlparse(pages[0].url).netloc.removeprefix("www.")

    def create(self, body: dict) -> dict:
        files = self.decode_files(body) if ("files" in body or "filename" in body or not body.get("web")) else []
        extra, notes, site = {}, [], ""
        if body.get("web"):
            more, extra, notes, site = self.read_web(body["web"])  # may take a while: the page says it is reading
            files += more
        return {"id": self.store.create_many(str(body.get("name") or site), files, extra), "notes": notes}

    def add_sources(self, pid: str, body: dict) -> dict:
        self.store.add_sources(pid, self.decode_files(body))
        return {"ok": True}

    def add_web(self, pid: str, body: dict) -> dict:
        files, extra, notes, _ = self.read_web(body)
        self.store.add_sources(pid, files, extra)
        return {"ok": True, "added": len(files), "notes": notes}

    def example(self) -> dict:
        return {"id": self.store.create("Bike shop (example)", "bike-shop.csv", (STATIC / "example-bike-shop.csv").read_text(encoding="utf-8"))}

    def project(self, pid: str, light: bool = False) -> dict:
        s = self.store
        if not light:
            s.ensure_isolation(pid)  # test questions found among the sources are moved out before anything reads them
        st = s.status(pid)
        if light:
            return {"status": st, "progress": s.progress(pid) if st["state"] == "training" else None, "export": s.export_status(pid)}
        meta = s.meta(pid)
        out = {"id": pid, "name": meta["name"], "filename": meta["filename"], "created": meta["created"], "gen": meta.get("gen", 0),
               "settings": {"target_precision": meta.get("target_precision", 0.95), "manual": meta.get("manual") or {}, "base": meta.get("base") or "minilm-l6", "finetune": meta.get("finetune", True)},
               "bases": [{"key": k, "label": BASE_NOTES.get(k, ""), "hf_id": b.hf_id, "languages": b.languages} for k, b in BASES.items()],
               "faq": self.faq_with_language(pid, meta), "status": st,
               "heldout": s.heldout_summary(pid), "regression": s.regression_summary(pid), "stale": bool(st["state"] == "trained" and meta.get("trained_signature") and meta["trained_signature"] != s.signature(pid)
                                                          and meta.get("indexed_signature") != s.signature(pid)),
               "reindexed": bool(st["state"] == "trained" and meta.get("trained_signature") and meta["trained_signature"] != s.signature(pid) and meta.get("indexed_signature") == s.signature(pid)),
               "progress": s.progress(pid) if st["state"] == "training" else None, "export": s.export_status(pid), "report": None, "thresholds": None, "feedback": None,
               "notices": meta.get("notices", []), "llm": self.llm(pid).public()}
        if st["state"] == "trained":
            out.update(report=s.report(pid), thresholds=s.thresholds(pid), feedback=s.judged_state(pid))
        return out

    def llm(self, pid: str) -> LLMSettings:
        return LLMSettings(self.store.dir(pid))

    @staticmethod
    def llm_providers() -> list[dict]:
        return [{"key": k, "label": v["label"], "needs_key": v["key"], "needs_address": v["custom_url"], "default_address": v["base_url"]} for k, v in llm_runtime.PROVIDERS.items()]

    def faq_with_language(self, pid: str, meta: dict) -> dict:
        """The summary of what was read, plus what language it seems to be in and, when that does not suit the chosen model, what to do."""
        faq = self.store.faq_summary(pid)
        found = language.detect_kb(self.store.kb(pid))
        faq["language"] = {**found, "advice": language.advice(found, BASES[meta.get("base") or "minilm-l6"].languages)}
        return faq

    def engine(self, pid: str) -> ProjectEngine:
        with self.engine_lock:
            eng = self.engines.get(pid)
            if eng is None or not eng.current():
                eng = self.engines[pid] = ProjectEngine(self.store.run_dir(pid))
                self._learn(pid, eng)  # questions approved earlier are searched from the first question on
            return eng

    def _learn(self, pid: str, eng: ProjectEngine) -> int:
        """Search every question a person approved or corrected as an extra phrasing of its entry now, without retraining. Returns how many are new."""
        with self.learn_lock:
            extras = fb.extra_phrasings(self.store.history(pid), set(eng.base_kb.index))
            return eng.learn(extras) if extras else 0

    def reindex(self, pid: str) -> dict:
        """Search the current files with the trained model as it is: seconds, no training. Unchanged vectors are kept; the model is not changed (training is how it learns new wording)."""
        st = self.store.status(pid)
        if st.get("state") == "training":
            raise ValueError("The model is being trained; wait for it to finish.")
        if st.get("state") != "trained":
            raise ValueError("Train the model first: there is no model to search with yet.")
        self.store.ensure_isolation(pid)
        kb = self.store.kb(pid)  # unreadable files were refused when they were added
        eng = self.engine(pid)
        with self.learn_lock:
            done = eng.reindex(kb)
        self._learn(pid, eng)  # questions approved earlier are searched again from the first question on
        self.store.mark_indexed(pid)
        return {"ok": True, **done}

    def train(self, pid: str, use_feedback: bool) -> dict:
        self.store.ensure_isolation(pid)
        self.engines.pop(pid, None)
        self.store.start(pid, "train", use_feedback)
        return {"ok": True}

    def test(self, pid: str, query: str) -> dict:
        if self.store.status(pid).get("state") != "trained":
            raise ValueError("Train the model first.")
        q = clean(query)
        if not q:
            raise ValueError("Type a question.")
        if len(q) > 500:
            raise ValueError("That question is longer than 500 characters.")
        th = self.store.thresholds(pid)
        conn = self.llm(pid).connection()  # None unless this business turned a model on and it is configured
        res = self.engine(pid).ask(q, th)
        tid = self.store.add_test(pid, {"query": q, "band": res["band"], "top_entry": res["hits"][0]["id"], "top_score": res["confidence"],
                                        "learned": bool(res["hits"][0].get("learned")),  # matched a question a person approved earlier: not evidence of accuracy
                                        "gen": self.store.meta(pid).get("gen", 0), "hits": [{"id": h["id"], "score": h["score"]} for h in res["hits"]]})
        return {**res, "response": self.engine(pid).respond(q, th, llm=conn, pipeline=self.pipeline), "test_id": tid, "query": q, "thresholds": {k: th.get(k) for k in ("floor", "direct", "source", "direct_reason", "labeled")}}

    def _recalibrate(self, pid: str) -> dict:
        meta = self.store.meta(pid)
        th = fb.recalibrate(self.store.current_history(pid), self.store.thresholds_auto(pid), meta.get("target_precision", 0.95))
        manual = meta.get("manual") or {}
        for key in ("floor", "direct"):  # a person's own setting always wins over anything calculated
            if manual.get(key) is not None:
                th[key], th["source"] = float(manual[key]), "manual"
        self.store.save_thresholds(pid, th)
        return th

    def feedback(self, pid: str, body: dict) -> dict:
        ids = {e["id"] for e in self.store.faq_summary(pid)["entry_list"]}
        self.store.add_feedback(pid, str(body.get("test_id", "")), str(body.get("verdict", "")), str(body.get("comment", "")), body.get("correct_entry"), ids)
        th = self._recalibrate(pid)
        try:
            added = self._learn(pid, self.engine(pid))  # an approved or corrected question joins what is searched right now
        except Exception:  # noqa: BLE001 - the judgement is saved; searching it is a bonus that must never fail the request
            traceback.print_exc()
            added = 0
        return {"feedback": self.store.judged_state(pid), "thresholds": th, "learned": added}

    def settings(self, pid: str, body: dict) -> dict:
        """target_precision (how sure before answering without an LLM), and the two lines themselves: floor ("not in the FAQ" below it)
        and direct (answer without an LLM from it). null resets a line to automatic."""
        changes = {}
        if "target_precision" in body:
            t = body["target_precision"]
            if not isinstance(t, (int, float)) or not 0.8 <= t <= 0.999:
                raise ValueError("target_precision must be between 0.8 and 0.999")
            changes["target_precision"] = float(t)
        if "finetune" in body:
            if not isinstance(body["finetune"], bool):
                raise ValueError("finetune must be true or false")
            changes["finetune"] = body["finetune"]
        if "base" in body:
            if body["base"] not in BASES:
                raise ValueError("Unknown model.")
            changes["base"] = body["base"]
        manual = dict(self.store.meta(pid).get("manual") or {})
        for key in ("floor", "direct"):
            if key in body:
                v = body[key]
                if v is not None and (not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.0 <= v <= 1.0):
                    raise ValueError(f"{key} must be a number from 0 to 1, or empty for automatic")
                manual[key] = None if v is None else float(v)
                changes["manual"] = manual
        if not changes:
            raise ValueError("Nothing to change.")
        self.store.set_meta(pid, **changes)
        return {"thresholds": self._recalibrate(pid) if self.store.status(pid).get("state") == "trained" else None}

    def heldout(self, pid: str, body: dict) -> dict:
        return {"heldout": self.store.set_heldout(pid, str(body.get("text") or ""))}


class Handler(BaseHTTPRequestHandler):
    app: App
    port: int
    protocol_version = "HTTP/1.1"

    # ---- plumbing
    def log_message(self, fmt, *args):
        if "/api/projects/" not in self.path or self.command != "GET":  # polling would drown everything else
            sys.stderr.write(f"{self.command} {self.path.split('?')[0]} {args[1] if len(args) > 1 else ''}\n")

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json")

    def _allowed(self) -> bool:
        """Only pages served from this server may use it (blocks other websites from talking to it)."""
        ok = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        if self.headers.get("Host", "") not in ok:
            return False
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).netloc in ok

    def _body(self) -> dict:
        if self.command == "POST" and "application/json" not in self.headers.get("Content-Type", ""):
            raise ValueError("JSON expected.")
        try:
            data = json.loads(self._raw) if self._raw else {}
        except ValueError:
            raise ValueError("The request was not valid JSON.") from None
        if not isinstance(data, dict):
            raise ValueError("The request must be a JSON object.")
        return data

    def _file(self, path: Path, ctype: str, name: str | None = None) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if name:
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.end_headers()
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                self.wfile.write(chunk)

    def _handle(self) -> None:
        # Every request body is read in full first, whether or not the route wants it: browsers reuse the connection, and
        # an unread body would be taken for the start of the next request.
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if not 0 <= n <= MAX_UPLOAD + 4096:
            self.close_connection = True
            return self._json(413 if n > 0 else 400, {"error": "That request is too large or malformed."})
        self._raw = self.rfile.read(n) if n else b""
        if not self._allowed():
            return self._json(403, {"error": "This page is not allowed to use the studio."})
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        try:
            self._route(path, q)
        except KeyError:
            self._json(404, {"error": "Not found."})
        except Busy as e:
            self._json(409, {"error": str(e)})
        except ValueError as e:
            self._json(400, {"error": str(e)})
        except BrokenPipeError:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            self._json(500, {"error": "Something went wrong inside the studio. The details are in its terminal window."})

    do_GET = do_POST = do_DELETE = _handle

    # ---- routes
    def _route(self, path: str, q: dict) -> None:
        app, store, m = self.app, self.app.store, None
        get, post, delete = self.command == "GET", self.command == "POST", self.command == "DELETE"
        if get and path in ("/", "/index.html"):
            return self._send(200, (STATIC / "index.html").read_bytes(), TYPES[".html"])
        if get and (m := re.fullmatch(r"/static/([a-z0-9._-]+)", path)) and (STATIC / m.group(1)).is_file():
            return self._send(200, (STATIC / m.group(1)).read_bytes(), TYPES.get(Path(m.group(1)).suffix, "application/octet-stream"))
        if get and path == "/api/overview":
            return self._json(200, app.overview())
        if get and path == "/api/template.csv":
            return self._send(200, TEMPLATE.encode(), TYPES[".csv"], {"Content-Disposition": 'attachment; filename="faq-template.csv"'})
        if get and path == "/api/llm/providers":
            return self._json(200, {"providers": app.llm_providers()})
        if post and path == "/api/projects":
            return self._json(200, app.create(self._body()))
        if post and path == "/api/projects/example":
            return self._json(200, app.example())
        if (m := re.fullmatch(rf"/api/projects/{PID}", path)):
            pid = m.group("pid")
            if get:
                return self._json(200, app.project(pid, light=q.get("light") == ["1"]))
            if delete:
                store.delete(pid)
                return self._json(200, {"ok": True})
        if (m := re.fullmatch(rf"/api/projects/{PID}/(?P<what>[a-z./]+)", path)):
            pid, what = m.group("pid"), m.group("what")
            store.dir(pid)
            if post and what == "train":
                return self._json(200, app.train(pid, bool(self._body().get("use_feedback"))))
            if post and what == "reindex":
                return self._json(200, app.reindex(pid))
            if post and what == "test":
                return self._json(200, app.test(pid, str(self._body().get("query") or "")))
            if post and what == "feedback":
                return self._json(200, app.feedback(pid, self._body()))
            if post and what == "settings":
                return self._json(200, app.settings(pid, self._body()))
            if get and what == "llm":
                return self._json(200, app.llm(pid).public())
            if post and what == "llm":
                return self._json(200, app.llm(pid).save(self._body()))
            if post and what == "llm/validate":
                return self._json(200, app.llm(pid).validate(self._body()))
            if post and what == "llm/skip":
                return self._json(200, app.llm(pid).skip())
            if post and what == "llm/remove":
                app.llm(pid).remove()
                return self._json(200, app.llm(pid).public())
            if post and what == "sources":
                return self._json(200, app.add_sources(pid, self._body()))
            if post and what == "web":
                return self._json(200, app.add_web(pid, self._body()))
            if post and what == "columns":
                return self._json(200, store.table_info(pid, str(self._body().get("name") or "")))
            if post and what == "columns/apply":
                body = self._body()
                store.apply_mapping(pid, str(body.get("name") or ""), body.get("mapping") if isinstance(body.get("mapping"), dict) else {})
                return self._json(200, {"ok": True})
            if post and what == "sources/remove":
                store.remove_source(pid, str(self._body().get("name") or ""))
                return self._json(200, {"ok": True})
            if post and what == "heldout":
                return self._json(200, app.heldout(pid, self._body()))
            if post and what == "regression":
                return self._json(200, {"regression": store.set_regression(pid, str(self._body().get("text") or ""))})
            if post and what == "regression/clear":
                store.clear_regression(pid)
                return self._json(200, {"ok": True})
            if post and what == "heldout/clear":
                store.clear_heldout(pid)
                return self._json(200, {"ok": True})
            if post and what == "export":
                if store.status(pid).get("state") != "trained":
                    raise ValueError("Train the model first.")
                store.start(pid, "export")
                return self._json(200, {"ok": True})
            if get and what == "package.zip":
                ex = store.export_status(pid)
                zp = store.dir(pid) / "package" / ex.get("zip", "missing")
                if ex.get("state") != "done" or not zp.is_file():
                    raise KeyError(pid)
                return self._file(zp, "application/zip", zp.name)
            if get and what == "feedback.csv":
                return self._send(200, store.feedback_csv(pid).encode(), TYPES[".csv"], {"Content-Disposition": 'attachment; filename="my-tests.csv"'})
            if get and what == "report.md":
                rp = store.run_dir(pid) / "report.md"
                if not rp.is_file():
                    raise KeyError(pid)
                return self._send(200, rp.read_bytes(), "text/markdown; charset=utf-8", {"Content-Disposition": 'attachment; filename="report.md"'})
        raise KeyError(path)


def serve(data: str = "studio_data", port: int = 8765, open_browser: bool = False) -> None:
    store = Store(data)
    Handler.app, Handler.port = App(store), port
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        raise SystemExit(f"Port {port} is already in use. If the studio is already running, open http://127.0.0.1:{port} ; otherwise pick another with --port.")
    url = f"http://127.0.0.1:{port}"
    print(f"FAQ studio is running at {url}  (your projects are kept in {Path(data).resolve()})\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="The FAQ studio: upload a FAQ, train, test, approve or reject.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", default="studio_data", help="where projects are kept")
    ap.add_argument("--open", action="store_true", help="open the page in your browser")
    a = ap.parse_args(argv)
    serve(a.data, a.port, a.open)


if __name__ == "__main__":
    main()
