"""Write extra ways of asking each FAQ question, using any OpenAI-compatible chat endpoint.

The measured gain from training comes from phrasing diversity (docs/RESULTS.md), and a business rarely has
more than one phrasing per entry, so this is the step that makes training worth running. It works with Gemini
(through its OpenAI-compatible address), Groq, OpenRouter, a local Ollama server, or OpenAI itself.

The API key is read from an environment variable, never from the command line, and is only ever sent to the
`--base-url` you give. Only the FAQ question and answer are sent, nothing else.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from .kb import Entry, clean

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_KEY_ENV = "GEMINI_API_KEY"
RETRY_STATUS = (408, 429, 500, 502, 503, 504)
MAX_LEN = 200

PROMPT = """You help a small business prepare its FAQ for a search engine.

FAQ question: {question}
{answer_line}
Write {n} different ways real customers might type this same question into a chat box. Rules:
- Keep the meaning of the FAQ question. Do not answer it and do not invent other questions.
- Vary the wording a lot: some short and keyword-like, some long and chatty, some casual, a few with small typos.
- Do not repeat the FAQ question or each other. Do not mention the business name.
- Reply with ONLY a JSON array of {n} strings."""


class LLMError(RuntimeError):
    pass


class ChatClient:
    """One method: complete(prompt) -> text. Retries rate limits and server errors with backoff."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, timeout: float = 60, retries: int = 5, delay: float = 0.0):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.timeout, self.retries, self.delay = model, api_key, timeout, retries, delay
        self._last = 0.0

    def complete(self, prompt: str, temperature: float = 0.9) -> str:
        body = json.dumps({"model": self.model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        wait = self._last + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                self._last = time.monotonic()
                return data["choices"][0]["message"]["content"] or ""
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:300]
                if e.code in RETRY_STATUS and attempt < self.retries:
                    time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))
                    continue
                raise LLMError(f"HTTP {e.code} from {self.url}: {detail}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.retries:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMError(f"could not reach {self.url}: {e}") from None
            except (KeyError, IndexError, ValueError) as e:
                raise LLMError(f"unexpected reply from {self.url}: {type(e).__name__}") from None
        raise LLMError("unreachable")


def client_from_env(base_url: str, model: str, key_env: str, delay: float) -> ChatClient:
    key = os.environ.get(key_env)
    local = base_url.startswith(("http://localhost", "http://127.0.0.1"))
    if not key and not local:
        raise LLMError(f"no API key: set the {key_env} environment variable in your own terminal (not on the command line)")
    return ChatClient(base_url, model, key, delay=delay)


def make_prompt(question: str, answer: str, n: int) -> str:
    return PROMPT.format(question=question, n=n, answer_line=f"FAQ answer: {answer}\n" if answer else "")


def parse(text: str) -> list[str]:
    """A JSON array of strings, with or without a code fence; or, failing that, one phrasing per line."""
    start, end = text.find("["), text.rfind("]")
    if 0 <= start < end:
        try:
            items = json.loads(text[start:end + 1])
            if isinstance(items, list):
                return [str(x) for x in items if isinstance(x, (str, int, float))]
        except ValueError:
            pass
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", ln) for ln in text.splitlines()]
    return [ln for ln in lines if ln.strip() and not ln.strip().startswith("```")]


def tidy(original: str, items: list[str], n: int) -> list[str]:
    """Drop empties, over-long text, the original and duplicates; keep at most n."""
    seen, out = {clean(original).lower()}, []
    for item in items:
        text = clean(item).strip("\"'`“”").strip()
        if not text or len(text) > MAX_LEN or text.lower() in seen:
            continue
        seen.add(text.lower())
        out.append(text)
    return out[:n]


class Cache:
    """On-disk results keyed by the question and answer, so a rerun, or a crash, never repeats a call."""

    def __init__(self, path: str | Path, model: str, n: int):
        self.path = Path(path)
        self.model, self.n = model, n
        self.items: dict[str, list[str]] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
            meta = data.get("meta", {})
            if (meta.get("model"), meta.get("n")) != (model, n):
                raise LLMError(f"{self.path} was made with model={meta.get('model')!r}, n={meta.get('n')}; use another --out for {model!r}, n={n}")
            self.items = data["items"]

    @staticmethod
    def key(question: str, answer: str) -> str:
        return hashlib.sha1(f"{clean(question)}\n{clean(answer)}".encode()).hexdigest()

    def get(self, question: str, answer: str) -> list[str] | None:
        return self.items.get(self.key(question, answer))

    def put(self, question: str, answer: str, paraphrases: list[str]) -> None:
        self.items[self.key(question, answer)] = paraphrases
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"meta": {"model": self.model, "n": self.n}, "items": self.items}, ensure_ascii=False, indent=1))
        tmp.replace(self.path)

    @classmethod
    def load(cls, path: str | Path) -> "Cache":
        meta = json.loads(Path(path).read_text())["meta"]
        return cls(path, meta["model"], meta["n"])


def generate(entries: list[Entry], n: int, ask: Callable[[str], str], cache: Cache, progress: Callable[[str], None] = print) -> dict:
    """Fill the cache for every entry's primary question. Returns counts."""
    done = fresh = short = 0
    for i, e in enumerate(entries, 1):
        if cache.get(e.question, e.answer) is not None:
            done += 1
            continue
        items = tidy(e.question, parse(ask(make_prompt(e.question, e.answer, n))), n)
        if len(items) < n:
            short += 1
        cache.put(e.question, e.answer, items)
        fresh += 1
        if fresh % 10 == 0 or i == len(entries):
            progress(f"  {i}/{len(entries)} entries ({fresh} asked, {done} already cached)")
    return {"entries": len(entries), "asked": fresh, "cached": done, "short": short}


def expand(entries: list[Entry], cache: Cache) -> list[Entry]:
    """The same entries with the generated phrasings appended after the originals."""
    out = []
    for e in entries:
        have = {q.lower() for q in e.questions}
        extra = [x for x in cache.get(e.question, e.answer) or [] if x.lower() not in have]
        out.append(Entry(e.id, [*e.questions, *extra], e.answer, e.category))
    return out
