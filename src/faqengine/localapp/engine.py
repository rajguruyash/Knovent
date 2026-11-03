"""THE REAL ENGINE FOR THE ONE-COMMAND APP. Reads each business's files with the same reader the hosted service uses, embeds them with the same MiniLM (the int8 ONNX model, no PyTorch),
searches them with the same hybrid index and answers with the same evidence checks. Only where things are kept differs from the hosted service: plain folders, no database.

An exact FAQ entry or a calculation is answered at once (the fast path: no model is asked). Anything else goes to the business's local model, when one was chosen, through the existing checked
path (every sentence it writes is verified against the evidence; a failed check falls back to the deterministic answer). With no model chosen the answer is deterministic."""
from __future__ import annotations

import re
import shutil
import threading
import time
from pathlib import Path

import numpy as np

from .. import web as webpage
from ..kb import load_kb
from ..runtime import answer, evidence, generate, safety, shadow, verify
from ..runtime import llm as L
from ..runtime.embedder import Embedder
from ..runtime.pipeline import Pipeline
from ..runtime.retrieval import CANDIDATES, DEFAULT_ALPHA, HybridIndex

ROOT = Path(__file__).resolve().parents[3]
MODEL_DIR = ROOT / "hosted_models" / "minilm-l6" / "model"
SECRET_FILE = re.compile(r"(^|[\\/])\.env($|\.)|\.env$|\.(pem|key|p12|pfx|keystore|jks)$|(^|[\\/])id_(rsa|dsa|ecdsa|ed25519)|(^|[\\/])(credentials|secrets?)(\.[a-z]+)?$", re.I)
OLLAMA_V1 = "http://127.0.0.1:11434/v1"
FLOOR = 0.40
ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class EngineError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


CHAT_OUTCOMES = ("faq", "grounded")
CITE = re.compile(r"\[\d+\]")


def _combine(llm, question: str, sources: list[dict], sim=None) -> dict:
    """The model answers from the retrieved pieces, in its own words, briefly and directly. Same safeguards as `generate.combine` (instruction-like text is refused, the pieces are fenced, the reply
    is verified against them), with a prompt written for small models: a direct answer first, two to four plain sentences, no restating of everything, no advice the pieces do not give."""
    if any(safety.injection_flags(x["text"]) for x in sources):
        return generate._result(None, "instruction_like_text", "grounded")
    body = "\n".join(f"[{x['ref']}] {safety.clean(x['text'])}" for x in sources)
    tag, block = safety.fence("passages", body)
    q_tag, q_block = safety.fence("question", safety.clean(question, 500))
    prompt = (f"Answer the customer's question using ONLY the numbered passages in {tag}.\n"
              "Rules: write two to four short, plain sentences. Start with the direct answer (say yes or no first if it is a yes or no question). "
              "If the passages give different answers for different cases, name each case in one short clause. Do not copy the passages, do not list every detail, do not repeat yourself, "
              "and do not add advice (such as contacting customer service) that no passage gives. Put the passage number like [1] after each sentence it supports. "
              "If the passages do not contain the answer, reply exactly: INSUFFICIENT.\n\n"
              f"Passages:\n{block}\n\nQuestion ({q_tag}):\n{q_block}\n\nAnswer:")
    text, failure = generate._ask(llm, prompt)
    if text is None:
        return generate._result(None, failure, "grounded")
    report = verify.verify(text, [{"ref": x["ref"], "text": x["text"]} for x in sources], "grounded", question, sim)
    return generate._result(text if report["passed"] else None, None if report["passed"] else "failed_verification", "grounded", report)


def _keep_supported(res: dict) -> dict:
    """When the check rejects some sentences of the model's reply and supports others, the reply is not thrown away: the rejected sentences are removed and the supported ones are kept, as the
    main pipeline does (`claims_removed`). Nothing the check did not support reaches the customer. A reply with no supported sentence stays rejected."""
    v = res.get("verification") or {}
    if res.get("text") or res.get("failure") != "failed_verification" or not v.get("claims"):
        return res
    sup = [c for c in v["claims"] if c.get("status") == "supported"]
    kept = sup
    if not kept:
        return res

    def sentence(c: dict) -> str:
        t = re.sub(r"\s+([.,;:!?])", r"\1", c["text"]).strip()
        t = t if t[-1:] in ".!?" else t + "."
        refs = " ".join(f"[{r}]" for r in (c.get("refs") or []) if not re.search(rf"\[{r}\]", t))
        return (t[:-1] + (" " + refs if refs else "") + t[-1]) if refs else t

    text = re.sub(r"\s{2,}", " ", " ".join(sentence(c) for c in sup))
    lead = re.match(r"^(However|But|Also|Additionally|Moreover|Still|Yet)\b[,:]?\s+", text)
    if lead and v["claims"][0].get("status") != "supported":  # the sentence this one answered was removed: it must not start mid-thought
        text = text[lead.end():][:1].upper() + text[lead.end():][1:]
    removed = len(v["claims"]) - len(kept)
    return {**res, "text": text, "failure": None, "verification": {**v, "passed": True, "claims_removed": removed, "note": f"{removed} sentence(s) the evidence did not support were removed"}}


class LocalEngine:
    def __init__(self, data_dir: str | Path, embedder: Embedder | None = None):
        self.root = Path(data_dir)
        self.embedder = embedder or Embedder(MODEL_DIR)
        self._idx: dict[str, HybridIndex] = {}
        self._lock = threading.RLock()

    # ---- files
    def _dir(self, business_id: str) -> Path:
        if not ID.match(business_id or ""):
            raise EngineError(400, "business_id must be an id")
        return self.root / "businesses" / business_id / "sources"

    def _files(self, business_id: str) -> list[Path]:
        d = self._dir(business_id)
        return sorted(d.glob("*/*")) if d.exists() else []

    def ingest(self, business_id: str, source_id: str, filename: str, data: bytes) -> dict:
        name = Path(filename).name
        if not ID.match(source_id or "") or not name:
            raise EngineError(400, "a source needs an id and a file name")
        if SECRET_FILE.search(name):
            raise EngineError(422, "this looks like a file of secrets (keys, passwords or certificates), not knowledge, so it was not read")
        d = self._dir(business_id) / source_id
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        (d / name).write_bytes(data)
        try:
            kb = load_kb(d / name)
        except Exception as e:  # noqa: BLE001 (a file that cannot be read says why, in words)
            shutil.rmtree(d, ignore_errors=True)
            raise EngineError(422, str(e)[:400] or "the file could not be read") from None
        self._rebuild(business_id)
        faqs = sum(e.kind == "faq" for e in kb.entries)
        return {"unchanged": False, "faqs": faqs, "chunks": len(kb.entries) - faqs, "version": 1, "warnings": kb.warnings[:5]}

    def ingest_url(self, business_id: str, source_id: str, url: str, whole: bool = False) -> dict:
        """A web address, read the way the hosted service reads it: never a private address, robots.txt obeyed, one page or up to ten of the same site, kept as small clean copies."""
        if not ID.match(source_id or "") or not url:
            raise EngineError(400, "a web source needs an id and an address")
        try:
            pages, _ = webpage.read_site(url, 10 if whole else 1)
        except (ValueError, webpage.WebError) as e:
            raise EngineError(422, str(e)[:400]) from None
        if not pages:
            raise EngineError(422, "no readable text was found at that address")
        d = self._dir(business_id) / source_id
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        paths = []
        for p in pages:
            f = d / Path(webpage.page_filename(p.url)).name
            f.write_bytes(webpage.page_html(p))
            paths.append(f)
        try:
            kb = load_kb(paths if len(paths) > 1 else paths[0])
        except Exception as e:  # noqa: BLE001
            shutil.rmtree(d, ignore_errors=True)
            raise EngineError(422, str(e)[:400] or "the page could not be read") from None
        self._rebuild(business_id)
        faqs = sum(e.kind == "faq" for e in kb.entries)
        return {"unchanged": False, "faqs": faqs, "chunks": len(kb.entries) - faqs, "version": 1, "pages": len(pages), "warnings": kb.warnings[:5]}

    def remove(self, business_id: str, source_id: str) -> dict:
        shutil.rmtree(self._dir(business_id) / source_id, ignore_errors=True)
        self._rebuild(business_id)
        return {"removed": True}

    # ---- the index
    def _rebuild(self, business_id: str) -> HybridIndex | None:
        paths = self._files(business_id)
        with self._lock:
            self._idx.pop(business_id, None)
        if not paths:
            return None
        kb = load_kb(paths if len(paths) > 1 else paths[0])
        entries = [{"id": e.id, "question": e.question, "answer": e.answer, "kind": e.kind, "source": e.source, "source_name": e.source, "category": e.category, "valid_from": e.valid_from,
                    "valid_until": e.valid_until, "supersedes": e.supersedes, **({"record": e.record} if e.record else {})} for e in kb.entries]
        views = [{"text": v.text, "entry": v.entry, "kind": v.kind} for v in kb.views]
        vectors = self.embedder.embed([v.text for v in kb.views], "doc")
        index = HybridIndex(entries, views, np.asarray(vectors, dtype=np.float32))
        with self._lock:
            self._idx[business_id] = index
        return index

    def _index(self, business_id: str) -> HybridIndex | None:
        with self._lock:
            have = self._idx.get(business_id)
        return have or self._rebuild(business_id)

    # ---- answers
    def _chunks(self, business_id: str, r: dict, limit: int = 3) -> list[dict]:
        """The best retrieved pieces of knowledge for this question (a FAQ as its question and answer, a passage as its text), best first, numbered so the answer can cite them."""
        with self._lock:
            index = self._idx.get(business_id)
        if index is None:
            return []
        by_id = {e["id"]: e for e in index.entries}
        cands = sorted(((r.get("retrieval") or {}).get("candidates") or []), key=lambda c: -float(c.get("combined_score") or 0))
        out: list[dict] = []
        for c in cands:
            e = by_id.get(c.get("entry_id"))
            if not e or any(x["id"] == e["id"] for x in out):
                continue
            text = f"Q: {e['question']} A: {e['answer']}" if e["kind"] == "faq" else e["answer"]
            if e["kind"] != "faq" and (text.count("##") >= 2 or len(text.split()) < 8):  # a list of headings or a stub is not evidence a model can use
                continue
            out.append({"ref": len(out) + 1, "kind": e["kind"], "id": e["id"], "source": e["source"], "location": e["source"] + (f" › {e['question']}" if e["kind"] == "faq" else ""), "text": text[:900]})
            if len(out) == limit:
                break
        return out

    def _word_it(self, r: dict, q: str, model: str, sim, business_id: str) -> None:
        """Chat style: the local model writes the answer from the best retrieved pieces of knowledge, every time something was retrieved. It is told to use ONLY those pieces and to say INSUFFICIENT
        if they do not contain the answer; its reply is verified against them (every number, date, price, time and condition must be theirs). A reply that fails, or INSUFFICIENT, leaves the engine's
        own answer (or its honest 'I do not know') in place, with the reason recorded. Where sources disagree the conflict is shown, not worded away."""
        cfg = L.LLMConfig(enabled=True, provider="openai_compatible", model=model, base_url=OLLAMA_V1, timeout=240.0, max_tokens=300)
        r["llm"] = {"enabled": True, "used": False, "capability": "grounded", "attempted": False, "failure": None, "provider": "openai_compatible", "model": model, "usage": {"calls": 0}}
        chunks = self._chunks(business_id, r) if r.get("outcome") in ("faq", "grounded", "partial", "insufficient") else []
        if not chunks:
            return
        r["llm"]["attempted"] = True
        llm = L.LLM(cfg, allow_private=True)
        res = _keep_supported(_combine(llm, q, chunks, sim))
        r["llm"]["usage"] = {"calls": 1}
        if not res.get("text") and len(chunks) > 1:  # a small model often refuses when given several pieces: ask once more with only the best one
            one = _keep_supported(_combine(llm, q, chunks[:1], sim))
            r["llm"]["usage"] = {"calls": 2}
            if one.get("text"):
                res, chunks = one, chunks[:1]
        if not res.get("text") and r.get("outcome") == "faq" and r.get("answer"):  # an exact FAQ match: reword the stored answer itself
            pol = generate.polish(llm, q, CITE.sub("", r["answer"]).strip(), sim)
            r["llm"]["usage"] = {"calls": r["llm"]["usage"]["calls"] + 1}
            if pol.get("text"):
                res = pol
                r["llm"]["capability"] = "polish"
        text = (res.get("text") or "").strip()
        if text and text.upper().startswith("INSUFFICIENT"):
            res, text = {"failure": "model_found_it_insufficient", "verification": None}, ""
        if text:
            r["answer"], r["verification"], r["message"] = text, res.get("verification"), None
            r["sources"] = [{k: v for k, v in c.items()} for c in chunks if f"[{c['ref']}]" in text] or chunks[:1]
            r["outcome"] = "grounded"
            r["llm"]["used"] = True
            r["execution"].update({"generated_by": "model", "answer_path": "model", "model_invoked": True, "inference_attempted": True, "inference_succeeded": True})
        elif res.get("failure") in ("declined", "model_found_it_insufficient") and r.get("outcome") != "faq":
            # the model read the retrieved pieces and says they do not contain the answer: that stands, instead of showing a loosely related passage as if it were the answer
            r["llm"]["failure"] = "model_found_it_insufficient"
            r["outcome"], r["answer"], r["sources"] = "insufficient", None, []
            r["message"] = "The information I have does not answer this. The most closely related material I found does not contain it. Would you like me to pass your question to a person?"
            r["execution"].update({"model_invoked": True, "inference_attempted": True, "inference_succeeded": True, "generated_by": "model", "answer_path": "abstained"})
        else:
            r["llm"]["failure"] = res.get("failure")
            r["execution"].update({"model_invoked": True, "inference_attempted": True, "inference_succeeded": False, "failure_kind": res.get("failure"), "fallback": True, "answer_path": "deterministic_fallback"})
            r["fallback"] = {"reason": res.get("failure"), "to": "deterministic"}

    def respond(self, business_id: str, query: str, model: str | None = None, style: str = "plain") -> dict:
        q = " ".join((query or "").split())
        if not q or len(q) > 2000:
            raise EngineError(400, "the question must be 1 to 2000 characters")
        started = time.perf_counter()
        index = self._index(business_id)
        if index is None:
            r = answer.respond(q, lambda _: ([], [], {"method": "none", "note": "no knowledge yet"}), None, llm=None)
            r["message"] = "There is no knowledge to search yet. Add a FAQ or a document first."
            r["execution"] = shadow.legacy_execution(r, None, None, "no knowledge")
            return r

        def search(text: str, on=None):
            t0 = time.perf_counter()
            qv = self.embedder.embed([text], "query")[0]
            embed_ms = (time.perf_counter() - t0) * 1000
            faq, passages, diag = index.search(text, qv, CANDIDATES, "hybrid", DEFAULT_ALPHA, on=on)
            diag["embed_ms"] = embed_ms
            return faq, passages, diag

        search.model = index.fact_model

        def sim(part: str, texts: list[str]) -> list[float]:
            return (self.embedder.embed_docs_cached(texts) @ self.embedder.embed([part], "query")[0]).tolist() if texts else []

        pipe = Pipeline("deterministic", fast_path=True)
        if model and style == "chat":
            r = pipe.run(q, search, sim, similarity_floor=FLOOR, sem_min=evidence.SEM_MIN)
            r["execution"] = {"path": "pipeline", **r["execution"], "tier": "local", "provider": "ollama", "model": model}
            self._word_it(r, q, model, sim, business_id)
            r["execution"]["latency_ms"] = int(1000 * (time.perf_counter() - started))
            return r
        if model:
            try:  # an exact answer the engine already has is not sent to a model (docs/fast-path.md)
                st = pipe.prepare(q, search, sim, None, FLOOR, evidence.SEM_MIN)
                decision = pipe.evaluate_fast_path(st)
                if decision.taken:
                    r = pipe.conclude_fast(st, decision)
                    r["execution"] = {"path": "pipeline", "tier": "local", **r["execution"]}
                    return r
            except Exception:  # noqa: BLE001 (the checked path below answers instead)
                pass
            cfg = L.LLMConfig(enabled=True, provider="openai_compatible", model=model, base_url=OLLAMA_V1, timeout=180.0, max_tokens=300)
            r = answer.respond(q, search, sim, L.LLM(cfg, allow_private=True), FLOOR, evidence.SEM_MIN)
            r["execution"] = {**shadow.legacy_execution(r), "tier": "local", "provider": "ollama", "model": model}
        else:
            r = pipe.run(q, search, sim, similarity_floor=FLOOR, sem_min=evidence.SEM_MIN)
            r["execution"] = {"path": "pipeline", **r["execution"]}
        r["execution"]["latency_ms"] = int(1000 * (time.perf_counter() - started))
        return r
