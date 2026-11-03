"""Everything a project can be made of: FAQ tables (csv, json, jsonl) and knowledge-base documents (txt, md, html, pdf, docx).

A FAQ row is a question with an answer. A document is cut into passages of about 80 words (the embedding model reads about 128 tokens),
each tagged with the nearest heading and the file it came from. Passages are searched together with the FAQ entries, but they are
never returned as a stored FAQ answer: they are searched (embedded, with keywords) and used as evidence, and an answer built from them quotes their own sentences with a reference. Training never uses them as FAQ pairs.

Nothing here knows what a FAQ is about. Column names are matched loosely (Question/Answer, Q/A, title/content, ...), so other people's
files work without renaming.
"""
from __future__ import annotations

import csv
import html
import html.parser
import io
import json
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .kb import ALT_SEP, KB, LONG_ANSWER_CHARS, Entry, clean
from .runtime.validity import inactive_reasons, parse_date

TABLE_SUFFIXES = (".csv", ".tsv", ".json", ".jsonl", ".xlsx")
DOC_SUFFIXES = (".txt", ".md", ".markdown", ".html", ".htm", ".pdf", ".docx")
SUPPORTED = TABLE_SUFFIXES + DOC_SUFFIXES
CHUNK_WORDS = 80
MAX_BYTES = 40 * 1024 * 1024  # what any one part of an office file may unpack to
MAX_TOTAL_BYTES = 200 * 1024 * 1024  # and what everything read from one file may unpack to in all (many sheets of 40 MB are not a way round the limit)

class Row(dict):
    """A row of a table keyed by normalised header, remembering the headers as the file spelled them (`headers`: key -> original text)."""

    headers: dict = {}


def _row(pairs: dict, headers: dict) -> "Row":
    r = Row(pairs)
    r.headers = headers
    return r


def _key(k: object) -> str:
    """A column name in a comparable form: lower case, no accents, anything that is not a letter or digit (in any alphabet) becomes _."""
    text = unicodedata.normalize("NFKD", str(k or "").strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[\W_]+", "_", text).strip("_")


def _keys(*names: str) -> tuple[str, ...]:
    return tuple(_key(n) for n in names)


# Column names, matched loosely and in several languages, so a file made anywhere works without renaming its header.
QUESTION_KEYS = _keys("question", "questions", "q", "faq", "faq_question", "query", "prompt", "pregunta", "preguntas", "frage", "fragen", "domanda", "domande", "pergunta",
                      "perguntas", "vraag", "vragen", "pertanyaan", "soal", "soru", "sorular", "问题", "問題", "質問", "질문", "प्रश्न", "सवाल", "вопрос", "вопросы")
ANSWER_KEYS = _keys("answer", "answers", "a", "response", "reply", "faq_answer", "content", "text", "body", "description", "details", "passage", "respuesta", "respuestas",
                    "antwort", "antworten", "risposta", "risposte", "resposta", "respostas", "antwoord", "reponse", "reponses", "jawaban", "cevap", "答案", "回答", "답변",
                    "उत्तर", "जवाब", "ответ", "ответы")
TEXT_KEYS = _keys("text", "content", "body", "passage", "description", "details", "texto", "contenido", "contenu", "texte", "inhalt", "contenuto", "conteudo", "tekst",
                  "konten", "icerik", "内容", "सामग्री", "текст")  # a table with these and no question column is a table of documents
TITLE_KEYS = _keys("title", "heading", "name", "subject", "topic", "titulo", "titre", "titel", "titolo", "judul", "baslik", "标题", "शीर्षक", "заголовок")
ID_KEYS = _keys("id", "faq_id", "slug", "key", "ref")
CATEGORY_KEYS = _keys("category", "section", "group", "tag", "tags", "categoria", "categorie", "kategorie", "kategori", "类别", "श्रेणी", "категория")
ALT_KEYS = _keys("alt_questions", "alternates", "alternate_questions", "alternative_questions", "variants", "paraphrases", "similar_questions", "other_questions",
                 "also_asked_as", "variantes")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "doc"


def _first(row: dict, keys: tuple[str, ...], skip: tuple = ()) -> tuple[str | None, object]:
    for k in keys:
        if k in row and k not in skip and row[k] not in (None, "", []):
            return k, row[k]
    return None, None


# ---------- tables

csv.field_size_limit(10_000_000)  # a cell is never legitimately larger than this, but a long answer is larger than the default 128 KB less often than you would think
LIST_KEYS = ("faqs", "faq", "entries", "items", "data", "documents", "passages", "rows", "records", "results", "questions", "mainEntity", "list")
WRAPPERS = ("attributes", "fields", "properties", "acf", "data")  # CMS exports (Strapi, WordPress, Contentful...) nest the real fields one level down


def _read_text(path: Path) -> str:
    """The file's text whatever program saved it: UTF-8 (with or without a BOM), UTF-16, or the Windows / Excel code page."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _delimited_rows(text: str) -> list[dict]:
    """CSV, TSV and the semicolon "CSV" that European Excel saves: the delimiter is whichever of , ; tab | the header row uses most."""
    header = text.lstrip("\ufeff").split("\n", 1)[0]
    delimiter = max(",;\t|", key=lambda d: (header.count(d), d == ","))
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    headers = {_key(k): k.strip() for k in (reader.fieldnames or []) if k}
    return [_row({_key(k): v for k, v in row.items() if k}, headers) for row in reader]


def _col(ref: str) -> int:
    """Column number of a spreadsheet cell reference: A1 -> 0, B7 -> 1, AA3 -> 26."""
    n = 0
    for ch in re.match(r"[A-Z]+", ref or "A").group():
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx_tables(path: Path) -> list[list[dict]]:
    """One list of rows (dicts keyed by the header row) per sheet that has a header and at least one more row."""
    return [rows for _, rows in _xlsx_sheets(path)]


def _xlsx_sheets(path: Path) -> list[tuple[str, list[dict]]]:
    """(sheet name, rows) for every sheet that has a header and at least one more row."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())

            budget = [0]

            def xml(name: str):
                size = z.getinfo(name).file_size
                budget[0] += size
                if size > MAX_BYTES or budget[0] > MAX_TOTAL_BYTES:
                    raise ValueError(f"{path.name}: the workbook is too large to read")
                data = z.read(name)
                if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                    raise ValueError(f"{path.name}: refused (the workbook declares XML entities)")
                return ET.fromstring(data)

            shared = ["".join(t.text or "" for t in si.iter(ns + "t")) for si in xml("xl/sharedStrings.xml").iter(ns + "si")] if "xl/sharedStrings.xml" in names else []
            targets = {r.get("Id"): r.get("Target") for r in xml("xl/_rels/workbook.xml.rels")}
            parts = []
            for sheet in xml("xl/workbook.xml").iter(ns + "sheet"):
                target = targets.get(sheet.get(rel + "id"), "")
                part = target.lstrip("/") if target.startswith("/") else "xl/" + target
                if part in names:
                    parts.append((sheet.get("name") or f"sheet{len(parts) + 1}", part))
            tables = []
            for sheet_name, part in parts:
                grid = []
                for row in xml(part).iter(ns + "row"):
                    cells = {}
                    for c in row.iter(ns + "c"):
                        kind = c.get("t")
                        if kind == "s":
                            value = shared[int(c.findtext(ns + "v") or 0)]
                        elif kind == "inlineStr":
                            value = "".join(t.text or "" for t in c.iter(ns + "t"))
                        else:
                            value = c.findtext(ns + "v") or ""
                        cells[_col(c.get("r"))] = value
                    if any(v.strip() for v in cells.values()):
                        grid.append(cells)
                if len(grid) >= 2:
                    keys = {i: _key(v) for i, v in grid[0].items() if v.strip()}
                    headers = {keys[i]: grid[0][i].strip() for i in keys}
                    tables.append((sheet_name, [_row({keys[i]: row.get(i, "") for i in keys}, headers) for row in grid[1:]]))
            return tables
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, ValueError) as e:
        if isinstance(e, ValueError) and "workbook" in str(e):
            raise
        raise ValueError(f"{path.name}: this is not a readable Excel (.xlsx) file") from None


def _flatten(row: dict) -> dict:
    """One flat row from the shapes other tools export: fields nested under "attributes", or a schema.org Question with its acceptedAnswer."""
    out = dict(row)
    for wrap in WRAPPERS:
        inner = out.get(wrap)
        if isinstance(inner, dict):
            out = {**inner, **{k: v for k, v in out.items() if k != wrap}}
    if str(out.get("@type", "")).lower() == "question":
        out.setdefault("question", out.get("name") or out.get("text"))
        answer = out.get("acceptedAnswer") or out.get("suggestedAnswer")
        answer = answer[0] if isinstance(answer, list) and answer else answer
        if isinstance(answer, dict):
            out.setdefault("answer", answer.get("text") or answer.get("name"))
    return out


def _table_rows(path: Path) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in (".csv", ".tsv"):
        return _delimited_rows(_read_text(path))
    if suffix == ".xlsx":
        tables = _xlsx_tables(path)
        faq = [t for t in tables if {k for r in t[:50] for k in r} & set(QUESTION_KEYS)]  # sheets that hold questions (one per category, say)
        return [r for t in (faq or tables[:1]) for r in t]
    text = _read_text(path)
    if suffix == ".jsonl":
        raw = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        data = json.loads(text)
        if isinstance(data, dict) and isinstance(data.get("@graph"), list):  # schema.org pages list their nodes under @graph
            data = next((n for n in data["@graph"] if isinstance(n, dict) and "mainEntity" in n), data)
        if isinstance(data, dict):
            data = next((data[k] for k in LIST_KEYS if isinstance(data.get(k), list)), None)
        raw = data
    if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
        raise ValueError(f"{path.name}: expected a list of objects (or an object with 'faqs', 'entries', 'items' or 'data')")
    return [_row({_key(k): v for k, v in _flatten(r).items()}, {_key(k): str(k) for k in _flatten(r)}) for r in raw]


def _text(value: object) -> str:
    """A cell as plain text: tags from a CMS or a scraped page are dropped and entities (&amp;) decoded."""
    s = str(value if value is not None else "")
    if "<" in s and re.search(r"</?[a-zA-Z][^>]*>", s):
        s = re.sub(r"<(script|style)\b.*?</\1>", " ", s, flags=re.S | re.I)
        s = re.sub(r"<\s*(br|/p|/li|/div|/h[1-6]|/tr)\b[^>]*>", " ", s, flags=re.I)
        s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s) if "&" in s else s


def _faq_entry(row: dict, n: int, source: str, cols: dict | None = None) -> Entry:
    """`cols` names the question and answer columns when the header does not (see infer_roles); otherwise the usual names are looked for."""
    cols = cols or {}
    qk, q = (cols["question"], row.get(cols["question"])) if cols.get("question") else _first(row, QUESTION_KEYS)
    questions: list[str] = []
    first = q if isinstance(q, list) else [q]
    alts = _first(row, ALT_KEYS)[1] or []
    if isinstance(alts, str):
        alts = alts.split(ALT_SEP)
    for text in [*first, *alts]:
        text = clean(_text(text))
        if text and text.lower() not in {x.lower() for x in questions}:
            questions.append(text)
    if not questions:
        raise ValueError(f"{source} row {n}: no question")
    _, answer = (None, row.get(cols["answer"])) if cols.get("answer") else _first(row, ANSWER_KEYS, skip=(qk,))
    answer = _text(answer)
    _, ident = _first(row, ID_KEYS)
    ident = re.sub(r"^(\d+)\.0$", r"\1", str(ident).strip()) if ident is not None else ident  # a spreadsheet stores 7 as 7.0
    _, cat = _first(row, CATEGORY_KEYS)
    if isinstance(cat, list):
        cat = ", ".join(map(str, cat))
    kind = "passage" if clean(row.get("kind")) == "passage" else "faq"
    entry = Entry(clean(ident) or f"faq-{n:03d}", questions, clean(answer), clean(cat) or None, kind, clean(row.get("source")) or source)
    for k in ("valid_from", "valid_until", "supersedes"):
        entry.__dict__[k] = clean(_text(row.get(k))) or None
    if isinstance(row.get("record"), dict):  # a saved knowledge base keeps the table rows as data
        entry.record = row["record"]
    return entry


TEST_MARKERS = ("entry_id", "split", "expected_behavior", "expected_behaviour", "expected_source_or_answer", "expected_source", "expected_answer", "expected_entry",
                "expected_entry_id", "gold", "gold_entry", "gold_id", "label", "abstain")  # columns only an evaluation file has: they say what the right answer to a question is
TEST_TYPES = {"paraphrase", "answer_terms", "typo", "short", "ambiguous", "out_of_scope_near", "out_of_scope_far", "out_of_scope", "multi_part", "multilingual"}


def test_question_columns(keys: set[str], rows: list[dict], has_question: bool | None = None) -> list[str]:
    """Why a table looks like test questions rather than FAQ entries (empty when it does not). Test questions must never become searchable
    FAQ entries: they would be found, trained on and indexed, and every evaluation of them would only measure the engine finding its own input."""
    if not (keys & set(QUESTION_KEYS) if has_question is None else has_question):
        return []
    found = [k for k in keys if k in TEST_MARKERS or k.startswith(("expected", "gold"))]
    kinds = [_key(r.get("type") or r.get("category") or "") for r in rows[:200]]
    if kinds and sum(k in TEST_TYPES for k in kinds) >= 0.5 * len(kinds):
        found.append("type")
    has_answer = any(r.get(a) not in (None, "") for r in rows[:50] for a in ANSWER_KEYS if a in r)
    return found if found and not (has_answer and found == ["label"]) else []


INTERROGATIVES = re.compile(r"^\s*(what|how|when|where|why|which|who|whom|whose|do|does|did|can|could|will|would|should|is|are|was|were|may|have|has)\b", re.I)
NOT_CONTENT = set(ID_KEYS + CATEGORY_KEYS + ALT_KEYS + _keys("kind", "source", "url", "link", "date", "created", "updated"))
CURRENCY_SUFFIX = {"usd": "USD", "inr": "INR", "eur": "EUR", "gbp": "GBP", "aud": "AUD", "cad": "CAD", "rs": "INR"}
SIZE_SUFFIX = {"kb": "KB", "mb": "MB", "gb": "GB", "tb": "TB"}
LABEL_EXPAND = {"max": "maximum", "min": "minimum", "avg": "average", "num": "number", "qty": "quantity", "pct": "percent", "amt": "amount"}
GENERIC_LABELS = {"name", "title", "item", "resource", "product", "entry", "id", "label", "key"}  # a heading column like these does not add its own name to the row's subject
TIME_SUFFIX = {"minutes", "minute", "mins", "min", "hours", "hour", "hrs", "days", "day", "weeks", "week", "months", "month", "years", "year"}


def _cells(rows: list[dict], key: str, limit: int = 200) -> list[str]:
    out = []
    for r in rows[:limit]:
        v = r.get(key)
        if v in (None, "", []):
            continue
        text = clean(_text(", ".join(map(str, v)) if isinstance(v, list) else v))
        if text:
            out.append(text)
    return out


def column_profile(rows: list[dict], key: str) -> dict | None:
    """What a column holds, from its values: how long, how varied, how many look like questions, how many are numbers."""
    vals = _cells(rows, key)
    if not vals:
        return None
    n = len(vals)
    return {"n": n, "avg": sum(map(len, vals)) / n, "unique": len({v.lower() for v in vals}) / n, "ask": sum(v.endswith(("?", "\uff1f")) for v in vals) / n,
            "interrogative": sum(bool(INTERROGATIVES.match(v)) for v in vals) / n, "numeric": sum(bool(re.fullmatch(r"[\d.,%$\u20ac\u00a3\u20b9\s/-]+", v)) for v in vals) / n}


def infer_roles(rows: list[dict]) -> dict:
    """Which column is the question, the answer, the text, the title, when the header does not say. Names are tried first (in many languages); then what the values look like:
    a column whose values are questions is the question column, the longest remaining prose column is the answer or the text, a short column of mostly different values names a row."""
    keys = list(dict.fromkeys(k for r in rows[:50] for k in r))
    prof = {k: p for k in keys if (p := column_profile(rows, k))}
    q = next((k for k in keys if k in QUESTION_KEYS and k in prof), None)
    if q is None:
        cand = [k for k, p in prof.items() if k not in NOT_CONTENT and p["avg"] <= 300 and (p["ask"] >= 0.5 or (p["interrogative"] >= 0.8 and p["unique"] >= 0.8))]
        q = max(cand, key=lambda k: (prof[k]["ask"], prof[k]["unique"]), default=None)
    rest = [k for k in prof if k != q and k not in NOT_CONTENT]
    a = next((k for k in keys if k in ANSWER_KEYS and k in rest), None)
    if a is None:
        prose = [k for k in rest if prof[k]["avg"] >= 25 and prof[k]["numeric"] < 0.5]
        a = max(prose, key=lambda k: prof[k]["avg"], default=None)
    t = next((k for k in keys if k in TEXT_KEYS and k in prof), None)
    if t is None:
        prose = [k for k in rest if prof[k]["avg"] >= 80 and prof[k]["numeric"] < 0.5]
        t = max(prose, key=lambda k: prof[k]["avg"], default=None)
    title = next((k for k in keys if k in TITLE_KEYS and k in prof and k != t), None)
    if title is None:
        short = [k for k in keys if k in prof and k not in NOT_CONTENT and k not in (t, q) and prof[k]["avg"] <= 60 and prof[k]["unique"] >= 0.9 and prof[k]["numeric"] < 0.5]
        title = short[0] if short else None
    return {"question": q, "answer": a, "text": t, "title": title}


def table_mode(rows: list[dict]) -> tuple[str, dict]:
    """How a table is read: 'faq' (a question and its answer per row), 'documents' (a title and a text per row) or 'records' (every column of a row becomes a "Column: value"
    sentence, so that a pricing table, a product list or a schedule can be searched and quoted)."""
    keys = {k for r in rows[:50] for k in r}
    roles = infer_roles(rows)
    if keys & set(QUESTION_KEYS) or roles["question"]:
        return "faq", roles
    if keys & set(TEXT_KEYS) or roles["text"]:
        return "documents", roles
    return "records", roles


def _labelled(key: str, value: object) -> tuple[str, str]:
    """One cell as a (label, value): 'monthly price', '20 USD'. A unit in the header (price_usd, duration_minutes, max_file_size_mb) goes with a bare number, so that the figure states what
    it is, and abbreviations in headers (max, min, avg, qty) are written out so that a question in full words finds them."""
    tokens = [t for t in key.split("_") if t]
    unit = None
    if len(tokens) > 1 and tokens[-1] in CURRENCY_SUFFIX:
        unit, tokens = CURRENCY_SUFFIX[tokens[-1]], tokens[:-1]
    elif len(tokens) > 1 and tokens[-1] in SIZE_SUFFIX:
        unit, tokens = SIZE_SUFFIX[tokens[-1]], tokens[:-1]
    elif len(tokens) > 1 and tokens[-1] in TIME_SUFFIX:
        unit, tokens = tokens[-1], tokens[:-1]
    text = clean(_text(", ".join(map(str, value)) if isinstance(value, list) else value)).rstrip(".")
    if unit and re.fullmatch(r"[\d.,]+", text):
        text = f"{text} {unit}"
    elif unit:  # the value is not a bare number ("varies"): the unit stays in the label
        tokens = tokens + [unit.lower()]
    tokens = [LABEL_EXPAND.get(t, t) for t in tokens]
    return (" ".join(tokens) or key), text


def _record_text(row: dict, columns: list[str], title_key: str | None, fallback: str, context: str = "", soft: bool = False) -> tuple[str, str, str, str]:
    """(the row's name, its text). Every sentence names the row ("Pro plan, seats: 5."), so each one can stand as evidence on its own: a question about the Pro plan's seats finds one
    sentence with the plan, the attribute and the figure, wherever the cell sits in the row. `context` (the sheet's name, say) is added to the subject: "API requests (Limits), pro: 600." """
    name, label = fallback, ""
    if title_key and row.get(title_key) not in (None, "", []):
        value = clean(_text(row[title_key]))
        label = _labelled(title_key, value)[0]
        name = value
    subject = name if not label or label.lower() in GENERIC_LABELS or label.lower() in name.lower() else f"{name} {label}"
    if context and not (soft and any(w.rstrip("s") in subject.lower() for w in context.split())):  # a file's name is added only when the row's own name does not already say what it is
        subject = f"{subject} ({context})"
    parts = []
    for k in columns:
        if k == title_key or row.get(k) in (None, "", []):
            continue
        lab, val = _labelled(k, row[k])
        parts.append(f"{subject}, {lab}: {val}.")
    return name, (" ".join(parts) if parts else f"{subject}."), subject, ("" if label.lower() in GENERIC_LABELS else label)


def _record_entries(rows: list[dict], path: Path, title_key: str | None, prefix: str | None = None, sheet: str | None = None) -> list[Entry]:
    columns = [k for k in dict.fromkeys(k for r in rows for k in r) if any(r.get(k) not in (None, "", []) for r in rows)]
    out: list[Entry] = []
    prefix = prefix or slug(path.stem)
    headers = getattr(rows[0], "headers", {}) if rows else {}
    where = sheet or " ".join(t for t in re.split(r"[^A-Za-z0-9]+", path.stem) if t).lower()  # what the rows are rows OF: the sheet's name, else the file's ("offices")
    for i, r in enumerate(rows, start=1):
        if all(r.get(k) in (None, "", []) for k in columns):
            continue
        name, text, subject, noun = _record_text(r, columns, title_key, f"{sheet or path.stem} row {i}", where, soft=not sheet)
        fields = []
        for k in columns:
            if k == title_key:
                continue
            if r.get(k) in (None, "", []):  # an empty cell is a fact too: the table has the column, and says nothing in it for this row
                fields.append({"header": headers.get(k, k), "key": k, "label": _labelled(k, "")[0], "value": "", "text": ""})
                continue
            lab, val = _labelled(k, r[k])
            fields.append({"header": headers.get(k, k), "key": k, "label": lab, "value": clean(_text(", ".join(map(str, r[k])) if isinstance(r[k], list) else r[k])), "text": val})
        made = _passages(text, f"{sheet}: {name}" if sheet else name, path.name, f"{prefix}-{i:03d}", titled=True)
        for n, e in enumerate(made):  # the row as data, on each passage that holds some of it (the empty cells on the first)
            mine = [f for f in fields if (f["text"] and f"{f['label']}: {f['text']}" in e.answer) or (not f["text"] and n == 0)]
            e.record = {"table": sheet or where, "entity": name, "noun": noun, "subject": subject, "fields": mine}
        out += made
    return out


def table_text(grid: list[list[str]]) -> str:
    """A table found inside a document (Markdown, Word, a web page) as sentences: its first row names the columns and every other row is read like a row of a values table, one paragraph
    each. A table without a usable header row is read cell by cell, row by row."""
    grid = [[clean(_text(c)) for c in row] for row in grid if any(clean(_text(c)) for c in row)]
    if not grid:
        return ""
    head = [_key(c) for c in grid[0]]
    if len(grid) >= 2 and all(head) and len(set(head)) == len(head) and not all(re.fullmatch(r"[\d.,%\s-]+", c) for c in grid[0]):
        rows = [dict(zip(head, r)) for r in grid[1:]]
        return "\n\n".join(_record_text(r, head, head[0], f"row {i}")[1] for i, r in enumerate(rows, start=1) if any(r.values()))
    return "\n\n".join(" | ".join(r) for r in grid)


def _rows_to_entries(rows: list[dict], path: Path, faq_n: int, prefix: str | None = None, sheet: str | None = None) -> list[Entry]:
    keys = {k for r in rows[:50] for k in r}
    mode, roles = table_mode(rows)
    flagged = test_question_columns(keys, rows, mode == "faq")
    if flagged:
        raise ValueError(f"{path.name} looks like a list of test questions (columns: {', '.join(sorted(flagged))}), not FAQ entries. Test questions must not be added as "
                         "FAQ entries or documents: put the file under \"Held-out test questions\" instead, where it is used only to measure the engine.")
    prefix = prefix or slug(path.stem)
    if mode == "faq":
        cols = {"question": roles["question"] if not keys & set(QUESTION_KEYS) else None,
                "answer": roles["answer"] if not any(k in keys and k != roles["question"] for k in ANSWER_KEYS) else None}  # only where the usual names are missing
        return [_faq_entry(r, faq_n + i, path.name, cols) for i, r in enumerate(rows, start=1)]
    if mode == "documents":  # no question column: a table of documents (title, text)
        out: list[Entry] = []
        text_key = next((k for k in TEXT_KEYS if k in keys), None) or roles["text"]
        title_key = next((k for k in TITLE_KEYS if k in keys), None) or roles["title"]
        for i, r in enumerate(rows, start=1):
            text, title = r.get(text_key), r.get(title_key) if title_key else None
            out += _passages(_text(text), clean(_text(title)) or sheet or path.stem, path.name, f"{prefix}-{i:03d}", titled=bool(clean(_text(title))))
        return out
    return _record_entries(rows, path, roles["title"], prefix, sheet)


def _table_entries(path: Path, faq_n: int) -> list[Entry]:
    if path.suffix.lower() == ".xlsx":  # every sheet is read, each as its own table (a workbook keeps different things on different sheets)
        sheets = _xlsx_sheets(path)
        if not sheets:
            raise ValueError(f"{path.name}: the file has no rows")
        out: list[Entry] = []
        for name, rows in sheets:
            out += _rows_to_entries(rows, path, faq_n + len(out), f"{slug(path.stem)}-{slug(name)}", name if len(sheets) > 1 else None)
        if not out:
            raise ValueError(f"{path.name}: the table has no values to read")
        return out
    try:
        rows = _table_rows(path)
    except ValueError as e:
        if path.suffix.lower() == ".json" and not isinstance(e, json.JSONDecodeError):
            return _json_entries(json.loads(_read_text(path)), path)  # not a list of records: structured data of another shape (a profile, a settings file)
        raise
    if not rows:
        raise ValueError(f"{path.name}: the file has no rows")
    out = _rows_to_entries(rows, path, faq_n)
    if not out:
        raise ValueError(f"{path.name}: the table has no values to read")
    return out


def _json_scalar(v: object) -> bool:
    return v is None or isinstance(v, (str, int, float, bool))


def _json_text(v: object) -> str:
    return "yes" if v is True else "no" if v is False else "" if v is None else str(v)


def _json_entries(data: object, path: Path) -> list[Entry]:
    """Structured data of any shape (nested objects, lists of records) as passages: each top-level object becomes a passage of sentences ("Product, version: 4.2."), and each list of
    records inside becomes one passage per record, named by its name-like field ("Automation module, included in: Pro and above.")."""
    stem = slug(path.stem)
    NAME = _keys("name", "title", "label", "city", "product", "plan", "sku", "id", "slug")
    sections: list[tuple[str, list[str]]] = []

    def human(k: str) -> str:
        return " ".join(t for t in _key(k).split("_") if t).capitalize()

    def sentence(subject: str, trail: list[str], value: object) -> str:
        label, text = _labelled("_".join(_key(t) for t in trail), _json_text(value))
        return f"{subject}, {label}: {text}."

    def leaves(obj: dict, subject: str, trail: list[str], records: list) -> list[str]:
        out = []
        for k, v in obj.items():
            if _json_scalar(v):
                if v not in (None, ""):
                    out.append(sentence(subject, trail + [k], v))
            elif isinstance(v, dict):
                out += leaves(v, subject, trail + [k], records)
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                records.append((subject, trail + [k], v))
            elif isinstance(v, list) and all(_json_scalar(x) for x in v):
                if v:
                    out.append(sentence(subject, trail + [k], ", ".join(_json_text(x) for x in v)))
            else:
                out.append(sentence(subject, trail + [k], json.dumps(v, ensure_ascii=False)[:300]))
        return out

    def add_records(records: list) -> None:
        for subject, trail, items in records:
            label = human("_".join(trail))
            singular = re.sub(r"s$", "", _key(trail[-1]).replace("_", " "))
            for i, item in enumerate(items, start=1):
                name_key = next((k for k in item if _key(k) in NAME and _json_scalar(item[k]) and item[k] not in (None, "")), None)
                name = clean(_json_text(item[name_key])) if name_key else f"{label} {i}"
                who = name if singular in name.lower() else f"{name} {singular}"
                inner: list = []
                sents = leaves({k: v for k, v in item.items() if k != name_key}, who, [], inner)
                sections.append((f"{label}: {name}", sents or [f"{who}."]))
                add_records(inner)

    records: list = []
    if isinstance(data, dict):
        general = [sentence(human(path.stem), [k], v) for k, v in data.items() if _json_scalar(v) and v not in (None, "")]
        if general:
            sections.append((human(path.stem), general))
        for k, v in data.items():
            if isinstance(v, dict):
                sents = leaves(v, human(k), [], records)
                if sents:
                    sections.append((human(k), sents))
            elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                records.append((human(path.stem), [k], v))
            elif isinstance(v, list) and not _json_scalar(v):
                continue
            elif isinstance(v, list) and v:
                sections.append((human(k), [sentence(human(k), [k], ", ".join(_json_text(x) for x in v))]))
    elif isinstance(data, list) and all(_json_scalar(x) for x in data):
        sections.append((human(path.stem), [", ".join(_json_text(x) for x in data) + "."]))
    add_records(records)
    out: list[Entry] = []
    for n, (heading, sents) in enumerate(sections, start=1):
        out += _passages(" ".join(sents), heading, path.name, f"{stem}-{n:03d}", titled=True)
    if not out:
        raise ValueError(f"{path.name}: no values to read in this JSON")
    return out


# ---------- choosing the columns by hand: what the Studio shows and applies when the guess is wrong

ROLE_NAMES = ("question", "answer", "id", "category", "alt", "title", "text")


def describe_table(path: str | Path) -> dict:
    """The columns of a table file, a few rows, and how it will be read as it stands."""
    path = Path(path)
    rows = _table_rows(path)
    if not rows:
        raise ValueError(f"{path.name}: the file has no rows")
    columns = list(dict.fromkeys(k for r in rows[:200] for k in r))
    mode, roles = table_mode(rows)
    first = lambda names: next((k for k in columns if k in names), None)  # noqa: E731
    detected = {"question": roles["question"], "answer": roles["answer"], "title": roles["title"], "text": roles["text"], "id": first(ID_KEYS), "category": first(CATEGORY_KEYS), "alt": first(ALT_KEYS)}
    sample = [{k: clean(_text(r.get(k)))[:90] for k in columns} for r in rows[:3]]
    return {"columns": columns, "rows": len(rows), "mode": mode, "detected": detected, "sample": sample}


def remap_table(path: str | Path, mapping: dict) -> bytes:
    """The table as a CSV whose headers say what each column is, according to the person's choices (`mapping`: {"as": "faq" | "documents" | "records", "question": column, ...}).
    Everything that reads sources afterwards (training, export, retraining) simply finds the usual names. Raises ValueError for a choice that cannot work."""
    path = Path(path)
    if path.suffix.lower() == ".xlsx" and len(_xlsx_sheets(path)) > 1:
        raise ValueError(f"{path.name} has several sheets, and each is read as its own table already; choosing columns applies to a single table, so save the sheet you want to change as its own CSV file.")
    rows = _table_rows(path)
    if not rows:
        raise ValueError(f"{path.name}: the file has no rows")
    columns = list(dict.fromkeys(k for r in rows for k in r))
    how = mapping.get("as")
    if how not in ("faq", "documents", "records"):
        raise ValueError("Choose how each row should be read: as a question and answer, as a document, or as a record.")
    pick = {}
    for role in ROLE_NAMES:
        col = mapping.get(role) or None
        if col is not None and col not in columns:
            raise ValueError(f"There is no column {col!r} in {path.name}.")
        pick[role] = col
    flat = lambda v: clean(_text(", ".join(map(str, v)) if isinstance(v, list) else v))  # noqa: E731
    out = io.StringIO(newline="")
    w = csv.writer(out)
    if how == "faq":
        if not pick["question"]:
            raise ValueError("Choose the column that holds the questions.")
        if pick["question"] == pick["answer"]:
            raise ValueError("The question and the answer cannot be the same column.")
        head = [h for h, c in (("id", pick["id"]), ("question", pick["question"]), ("answer", pick["answer"]), ("category", pick["category"]), ("alt_questions", pick["alt"])) if c]
        w.writerow(head)
        for r in rows:
            w.writerow([flat(r.get(c)) if c != pick["alt"] else ALT_SEP.join(x.strip() for x in re.split(r"[|;\n]", flat(r.get(c))) if x.strip())
                        for c in (pick["id"], pick["question"], pick["answer"], pick["category"], pick["alt"]) if c])
    elif how == "documents":
        if not pick["text"]:
            raise ValueError("Choose the column that holds the text.")
        w.writerow(["title", "text"] if pick["title"] else ["text"])
        for r in rows:
            w.writerow(([flat(r.get(pick["title"]))] if pick["title"] else []) + [flat(r.get(pick["text"]))])
    else:
        w.writerow(["title", "text"])
        for i, r in enumerate(rows, start=1):
            if any(r.get(k) not in (None, "", []) for k in columns):
                name, text = _record_text(r, columns, pick["title"], f"{path.stem} row {i}")[:2]
                w.writerow([name, text])
    return out.getvalue().encode("utf-8")


# ---------- documents

class _Html(html.parser.HTMLParser):
    """Visible text of a web page, with headings marked "# ". Menus, footers, forms and scripts are left out, and when the page has a
    <main> or <article> with real text in it, only that is kept. Also collects the title, the links, and the address a stored copy
    of a web page came from (<meta name="source-url">)."""

    BLOCKS = {"p", "div", "li", "tr", "br", "section", "article", "main", "ul", "ol", "table", "blockquote", "pre"}
    SKIP = {"script", "style", "head", "nav", "noscript", "footer", "aside", "form", "svg", "iframe", "template", "button", "select", "dialog"}
    VOID = {"br", "img", "meta", "link", "input", "hr", "source", "area", "base", "col", "embed", "param", "track", "wbr"}
    MAIN = {"main", "article"}
    MAIN_WORDS = 40  # a <main> with fewer words than this is not the page's content

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self.main_lines: list[str] = []
        self.cur: list[str] = []
        self.skipping: list[str] = []  # the open tags whose content is left out
        self.main = 0
        self.title = ""
        self.source_url = ""
        self.links: list[str] = []
        self.in_title = False
        self.grid: list[list[str]] | None = None  # the table being read, row by row
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def _end(self, prefix: str = "") -> None:
        text = " ".join("".join(self.cur).split())
        if text:
            self.lines.append(prefix + text)
            if self.main:
                self.main_lines.append(prefix + text)
        self.cur = []

    def _table_start(self, tag: str) -> None:
        if tag == "table" and self.grid is None:
            self._end()
            self.grid = []
        elif self.grid is not None and tag == "tr":
            self.row = []
        elif self.grid is not None and self.row is not None and tag in ("td", "th"):
            self.cell = []

    def _table_end(self, tag: str) -> None:
        if self.grid is None:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.grid.append(self.row)
            self.row = None
        elif tag == "table":
            for para in [x for x in table_text(self.grid).split("\n\n") if x.strip()]:
                self.lines.append(para)
                if self.main:
                    self.main_lines.append(para)
            self.grid = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if not self.skipping:
            self._table_start(tag)
        if tag == "meta" and (a.get("name") or "").lower() == "source-url":
            self.source_url = (a.get("content") or "").strip()
        elif tag == "title":
            self.in_title = True
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])
        if tag in self.VOID:
            if tag == "br" and not self.skipping:
                self._end()
            return
        if tag in self.SKIP:
            self.skipping.append(tag)
        elif self.skipping:
            return
        elif re.fullmatch(r"h[1-6]", tag):
            self._end()
        elif tag in self.BLOCKS:
            self._end()
        if tag in self.MAIN and not self.skipping:
            self.main += 1

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if not self.skipping:
            self._table_end(tag)
        if tag in self.SKIP:
            if tag in self.skipping:  # closes this one and anything left open inside it
                del self.skipping[len(self.skipping) - 1 - self.skipping[::-1].index(tag):]
            return
        if self.skipping:
            return
        if re.fullmatch(r"h[1-6]", tag):
            self._end("# ")
        elif tag in self.BLOCKS:
            self._end()
        if tag in self.MAIN:
            self.main = max(0, self.main - 1)

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif self.cell is not None and not self.skipping:
            self.cell.append(data)
        elif not self.skipping:
            self.cur.append(data)

    def body_lines(self) -> list[str]:
        """The page's text: what is inside <main>/<article> when that is substantial, otherwise everything visible."""
        return self.main_lines if len(" ".join(self.main_lines).split()) >= self.MAIN_WORDS else self.lines


def read_html(text: str) -> _Html:
    parser = _Html()
    parser.feed(text)
    parser.close()
    parser._end()
    return parser


def _docx_text(path: Path) -> str:
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > MAX_BYTES:
                raise ValueError(f"{path.name}: the document is too large to read")
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError):
        raise ValueError(f"{path.name}: this is not a readable Word (.docx) file") from None
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError(f"{path.name}: refused (the document declares XML entities)")
    lines = []
    root = ET.fromstring(xml)
    body = root.find(ns + "body")

    def paragraph(p) -> None:
        text = "".join(t.text or "" for t in p.iter(ns + "t")).strip()
        style = p.find(f"{ns}pPr/{ns}pStyle")
        name = (style.get(ns + "val") if style is not None else "") or ""
        if text:
            lines.append(("# " if name.lower().startswith(("heading", "title")) else "") + text)

    if body is None:  # an unusual file: every paragraph, wherever it is
        for p in root.iter(ns + "p"):
            paragraph(p)
    else:
        for child in body:
            if child.tag == ns + "p":
                paragraph(child)
            elif child.tag == ns + "tbl":  # a table keeps its rows and columns: the header row names the cells below it
                grid = [["".join(t.text or "" for t in tc.iter(ns + "t")).strip() for tc in tr.findall(ns + "tc")] for tr in child.findall(ns + "tr")]
                if table_text(grid):
                    lines.append(table_text(grid))
    return "\n\n".join(lines)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("Reading PDFs needs the pypdf package: pip install pypdf") from None
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ValueError(f"{path.name}: the PDF is password protected")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"{path.name}: could not read this PDF ({type(e).__name__})") from None
    if not text.strip():
        raise ValueError(f"{path.name}: the PDF has no readable text (a scan of images needs OCR first)")
    return text


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        return path.read_text(encoding="utf-8-sig", errors="replace")
    if suffix in (".html", ".htm"):
        return "\n\n".join(read_html(path.read_text(encoding="utf-8", errors="replace")).body_lines())
    if suffix == ".docx":
        return _docx_text(path)
    if suffix == ".pdf":
        return _pdf_text(path)
    raise ValueError(f"{path.name}: unsupported file type")


def _plain(text: str) -> str:
    text = re.sub(r"```[a-zA-Z]*", " ", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links and images keep their label
    text = re.sub(r"[*_`>]+|\|", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_long(text: str, limit: int) -> list[str]:
    out, cur = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = sentence.split()
        while len(words) > limit:  # a single very long sentence
            if cur:
                out.append(" ".join(cur))
                cur = []
            out.append(" ".join(words[:limit]))
            words = words[limit:]
        if len(cur) + len(words) > limit and cur:
            out.append(" ".join(cur))
            cur = []
        cur += words
    if cur:
        out.append(" ".join(cur))
    return out


def chunk_text(text: str, title: str, limit: int = CHUNK_WORDS) -> list[tuple[str, str]]:
    """(heading, body) pieces of at most about `limit` words; a heading line (# ...) starts a new piece."""
    chunks: list[tuple[str, str]] = []
    heading, buf, buf_heading, count = title, [], title, 0

    def flush() -> None:
        nonlocal buf, count
        body = " ".join(buf).strip()
        if body:
            chunks.append((buf_heading, body))
        buf, count = [], 0

    for block in (b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()):
        lines = block.splitlines()
        if lines[0].lstrip().startswith("#"):
            flush()
            heading = _plain(re.sub(r"^\s*#+\s*", "", lines[0])) or title
            block = "\n".join(lines[1:]).strip()
            if not block:
                continue
        para = _plain(block)
        words = para.split()
        if not words:
            continue
        if len(words) > limit:
            flush()
            chunks += [(heading, piece) for piece in _split_long(para, limit)]
            continue
        if count + len(words) > limit:
            flush()
        if not buf:
            buf_heading = heading
        buf.append(para)
        count += len(words)
    flush()
    return chunks


def _passages(text: str, title: str, source: str, prefix: str, titled: bool = False) -> list[Entry]:
    """`titled`: the title belongs to this text alone (a row of a table), so it is the passage's heading even when there are no others."""
    out = []
    for i, (heading, body) in enumerate(chunk_text(text, title), start=1):
        question = heading if (heading != title or titled) else " ".join(body.split()[:12])  # a document without headings: lead with the first words
        out.append(Entry(f"{prefix}-{i:03d}", [clean(question)], clean(body), None, "passage", source))
    return out


FRONT_KEYS = {"valid_until": ("valid_until", "expires", "expiry", "until", "valid_to"), "valid_from": ("valid_from", "effective", "effective_from", "from", "valid_since"),
              "supersedes": ("supersedes", "replaces", "replaced")}


def split_front_matter(text: str) -> tuple[dict, str]:
    """A leading block between two lines of --- with simple `key: value` lines: what a document says about itself (when it stops being valid, what it replaces). Returns
    ({valid_until, valid_from, supersedes, title...}, the text without the block). Text without such a block is returned as it is."""
    m = re.match(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", text, re.S)
    if not m or len(m.group(1).splitlines()) > 30:
        return {}, text
    raw = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip():
            raw[_key(k)] = v.strip().strip("'\"")
    if not raw:
        return {}, text
    meta = {canon: next((raw[a] for a in names if raw.get(a)), None) for canon, names in FRONT_KEYS.items()}
    meta["title"] = raw.get("title")
    return {k: v for k, v in meta.items() if v}, text[m.end():]


def _md_tables(text: str) -> str:
    """Markdown pipe tables as sentences (see table_text); everything else unchanged."""
    lines, out, i = text.splitlines(), [], 0
    cells = lambda ln: [c.strip() for c in ln.strip().strip("|").split("|")]  # noqa: E731
    divider = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and divider.match(lines[i + 1]):
            grid, j = [cells(lines[i])], i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                grid.append(cells(lines[j]))
                j += 1
            out += ["", table_text(grid), ""]
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


QA_START = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*|__)?\s*(?:Q|Question)\s*[:.)]\s*(?:\*\*|__)?\s*(.+?)\s*(?:\*\*|__)?\s*$", re.I)
QA_ANSWER = re.compile(r"^\s*(?:\*\*|__)?\s*(?:A|Answer)\s*[:.)]\s*(?:\*\*|__)?\s*", re.I)


def _qa_pairs(text: str) -> tuple[list[tuple[str, str, str | None]], str]:
    """A document written as question and answer blocks (`**Q: How long?**` followed by the answer, under headings) is a FAQ, not prose: each block is returned as (question, answer,
    heading), and the rest of the text (the introduction, anything that is not a block) separately. Fewer than two blocks means it is not written that way and nothing is taken out."""
    pairs: list[tuple[str, str, str | None]] = []
    rest: list[str] = []
    heading: str | None = None
    cur: list | None = None
    for line in text.splitlines():
        m = QA_START.match(line)
        h = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m and len(m.group(1)) > 3:
            cur = [m.group(1).strip(" *_"), [], heading]
            pairs.append(cur)  # type: ignore[arg-type]
        elif h:
            heading, cur = h.group(1).strip(" *_"), None
            rest.append(line)
        elif cur is not None:
            if line.strip():
                cur[1].append(QA_ANSWER.sub("", line.strip(), count=1) if not cur[1] else line.strip())
        else:
            rest.append(line)
    out = [(q, " ".join(a).strip(), hd) for q, a, hd in pairs if " ".join(a).strip()]
    if len(out) < 2:
        return [], text
    return out, "\n".join(rest)


def _document_entries(path: Path) -> list[Entry]:
    label, title = path.name, ""
    if path.suffix.lower() in (".html", ".htm"):
        page = read_html(path.read_text(encoding="utf-8", errors="replace"))
        text, title = "\n\n".join(page.body_lines()), " ".join(page.title.split())
        label = page.source_url or label  # a stored copy of a web page is labelled with the address it was read from
        meta = {}
    else:
        text = read_document(path)
        meta = {}
        if path.suffix.lower() in (".txt", ".md", ".markdown"):
            meta, text = split_front_matter(text)
            text = _md_tables(text) if path.suffix.lower() != ".txt" else text
    pretty = meta.get("title") or title or re.sub(r"[_\-]+", " ", path.stem).strip().title()
    qa: list[tuple[str, str, str | None]] = []
    if path.suffix.lower() in (".txt", ".md", ".markdown"):
        qa, text = _qa_pairs(text)  # a question and answer document is read as a FAQ
    entries = [Entry(f"{slug(path.stem)}-qa-{i:03d}", [q], a, hd, "faq", label) for i, (q, a, hd) in enumerate(qa, 1)]
    if text.strip():
        entries += _passages(text, pretty, label, slug(path.stem))
    if not entries:
        raise ValueError(f"{label}: no text found in this file")
    for e in entries:  # what the document says about itself belongs to every passage of it
        for k in ("valid_from", "valid_until", "supersedes"):
            e.__dict__[k] = meta.get(k)
    return entries


# ---------- the whole project

def resolve_validity(entries: list[Entry], as_of: str | None = None) -> dict[str, str]:
    """Marks the entries that are not current on `as_of` (today if None): expired, not yet valid, or replaced by another source. Returns {entry id: reason}."""
    reasons = inactive_reasons([{"id": e.id, "source": e.source, "valid_from": e.valid_from, "valid_until": e.valid_until, "supersedes": e.supersedes} for e in entries], as_of)
    for e in entries:
        e.inactive = reasons.get(e.id)
    return reasons


def load_sources(paths: list[str | Path] | str | Path) -> KB:
    """FAQ tables and documents, in the order given, as one knowledge base."""
    paths = [paths] if isinstance(paths, (str, Path)) else list(paths)
    if not paths:
        raise ValueError("add at least one file")
    entries: list[Entry] = []
    warnings: list[str] = []
    seen: dict[str, str] = {}
    faq_n = 0
    for p in paths:
        path = Path(p)
        suffix = path.suffix.lower()
        if suffix in TABLE_SUFFIXES:
            found = _table_entries(path, faq_n)
        elif suffix in DOC_SUFFIXES:
            found = _document_entries(path)
        else:
            raise ValueError(f"{path.name}: unsupported file type. Use {', '.join(SUPPORTED)}")
        faq_n += sum(1 for e in found if e.kind == "faq" and e.id.startswith("faq-"))
        passages = sum(1 for e in found if e.kind == "passage")
        if passages and passages == len(found):
            as_table = path.suffix.lower() in TABLE_SUFFIXES
            warnings.append(f"{found[0].source}: " + (f"read as a table of values, each row a passage ({passages} in all)" if as_table else f"read as a document, cut into {passages} passage{'s' if passages != 1 else ''}")
                            + ". They are searched, by meaning and by keywords, together with your FAQ, and answers are built from them as evidence: the passage's own sentences, with a reference.")
        for e in found:
            if e.id in seen:
                if seen[e.id] == path.name:
                    raise ValueError(f"duplicate id: {e.id}")
                e.id = f"{slug(path.stem)}:{e.id}"
                if e.id in seen:
                    raise ValueError(f"duplicate id: {e.id}")
            seen[e.id] = path.name
            entries.append(e)
    firsts: dict[str, str] = {}
    for e in entries:
        if e.kind != "faq":
            continue
        key = e.question.lower()
        if key in firsts:
            warnings.append(f"{e.id} and {firsts[key]} have the same question: {e.question!r}")
        firsts.setdefault(key, e.id)
        if len(e.answer) > LONG_ANSWER_CHARS:
            warnings.append(f"{e.id}: long answer; only its first ~100 words are embedded in the combined view")
        if not e.answer:
            warnings.append(f"{e.id}: no answer; it can be found but there is nothing to return")
    for src in dict.fromkeys(e.source for e in entries):  # what a source says about when it is valid
        mine = [e for e in entries if e.source == src]
        for k in ("valid_from", "valid_until"):
            raw = next((getattr(e, k) for e in mine if getattr(e, k)), None)
            if raw and not parse_date(raw):
                warnings.append(f"{src}: {k.replace('_', ' ')} {raw!r} is not a date (write it as 2026-12-31), so it is ignored")
    resolve_validity(entries)  # as of today
    for src, why in {e.source: e.inactive for e in entries if e.inactive}.items():
        warnings.append(f"{src}: {why}. It is kept in the project, but never searched and never used to answer.")
    return KB(entries, warnings)


def kind_of(path: str | Path) -> str:
    """What a file will be read as: 'faq' (a table with questions), 'records' (a table of values, each row a passage) or 'document'."""
    path = Path(path)
    if path.suffix.lower() in DOC_SUFFIXES:
        return "document"
    try:
        mode, _ = table_mode(_table_rows(path))
    except ValueError as e:
        if path.suffix.lower() == ".json" and not isinstance(e, json.JSONDecodeError):
            return "records"  # structured data that is not a list of records
        raise
    return {"faq": "faq", "documents": "document", "records": "records"}[mode]
