"""STRUCTURED REASONING over table rows. A controlled layer, not a model: it answers the questions about a table of values that finding one sentence cannot ("which plan is cheapest?", "which
plans include WhatsApp?", "what is the difference between Pro and Business?", "can I have 10 seats on Pro?", "how much do 3 Pro plans cost?") by comparing, filtering, counting, adding and
multiplying the values of the rows that were read from the business's own files.

What it promises:
  * every number it uses is a cell of a row of the knowledge base (`record` of an entry, see sources._record_entries), and every result names the entries those cells are in;
  * a value is never invented, and a unit is never assumed: "Contact us", an empty cell, or a figure in another unit or currency is listed as not compared, and the answer says so;
  * a header is read as what it says (a sheet's own words), with a small list of synonyms; a column whose name sounds like "which plans" counts as saying which plans only if its values are
    plan names; otherwise the answer says what the column holds and that it does not name plans;
  * what it cannot decide it does not decide: it returns None (the ordinary evidence engine then answers or declines) or a result that says what is missing.

Standard library only; the exported package uses it. It never calls a model, and a model is never needed for it.
"""
from __future__ import annotations

import re

try:
    from .textutil import STOP, stem
except ImportError:  # an exported package puts runtime/ on the path
    from textutil import STOP, stem

# ---------- words

W = re.compile(r"[^\W_]+")
CURRENCY = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "usd": "USD", "eur": "EUR", "gbp": "GBP", "inr": "INR", "rs": "INR", "dollars": "USD", "dollar": "USD", "euros": "EUR", "euro": "EUR",
            "pounds": "GBP", "pound": "GBP", "rupees": "INR", "rupee": "INR"}
DATA = {"kb": 1e-6, "mb": 1e-3, "gb": 1.0, "tb": 1e3, "pb": 1e6}
TIME = {"min": 1.0, "mins": 1.0, "minute": 1.0, "minutes": 1.0, "hour": 60.0, "hours": 60.0, "hr": 60.0, "hrs": 60.0, "day": 1440.0, "days": 1440.0, "week": 10080.0, "weeks": 10080.0,
        "month": 43200.0, "months": 43200.0, "year": 525600.0, "years": 525600.0}
NOT_A_NUMBER = {"", "-", "—", "n/a", "na", "none", "tbd", "tba", "contact us", "contact sales", "contact", "custom", "custom pricing", "ask", "varies", "on request", "request a quote"}
UNLIMITED = {"unlimited", "no limit", "infinite", "unmetered", "∞"}


COMPOUNDS = [(re.compile(p, re.I), r) for p, r in ((r"\bset[- ]up\b", "setup"), (r"\blog[- ]in\b", "login"), (r"\bsign[- ]up\b", "signup"), (r"\bcheck[- ]out\b", "checkout"))]


def _plain(text: str) -> str:
    for pat, r in COMPOUNDS:
        text = pat.sub(r, text)
    return text.lower()


def words(text: str) -> list[str]:
    """Every word of a text in its stem form (stop words kept: a header like 'available on' is made of them)."""
    return [stem(w) for w in W.findall(_plain(text))]


def content(text: str) -> list[str]:
    return [stem(w) for w in W.findall(_plain(text)) if w not in STOP and (len(w) > 1 or w.isdigit())]


class Value:
    """A cell: a number with the unit it was written in, 'unlimited', words that are not a number ("Contact us"), or nothing."""

    def __init__(self, text: str):
        self.raw = (text or "").strip()
        self.kind, self.num, self.unit, self.dim, self.per = "missing", None, "", "", ""
        t = self.raw.lower().strip(" .")
        if not t:
            return
        if t in UNLIMITED:
            self.kind = "unlimited"
            return
        if t in NOT_A_NUMBER:
            self.kind = "text"
            return
        m = re.fullmatch(r"(?P<cur>[$€£₹])?\s*(?P<n>\d[\d,]*(?:\.\d+)?)\s*(?P<u>[a-z%$€£₹]+(?:\s[a-z]+)?)?\s*(?:/|per\s)\s*(?P<per>[a-z]+)?|(?P<cur2>[$€£₹])?\s*(?P<n2>\d[\d,]*(?:\.\d+)?)\s*(?P<u2>[a-z%]+)?", t)
        if not m:
            self.kind = "text"
            return
        n = (m.group("n") or m.group("n2")).replace(",", "")
        unit = (m.group("u") or m.group("u2") or "").strip()
        cur = m.group("cur") or m.group("cur2")
        self.num, self.per = float(n), (m.group("per") or "")
        self.kind = "number"
        if cur:
            self.dim, self.unit = "money", CURRENCY[cur]
        elif unit in CURRENCY:
            self.dim, self.unit = "money", CURRENCY[unit]
        elif unit in DATA:
            self.dim, self.unit, self.num = "data", "GB", self.num * DATA[unit]
        elif unit in TIME:
            self.dim, self.unit, self.num = "time", "min", self.num * TIME[unit]
        elif unit == "%":
            self.dim, self.unit = "percent", "%"
        elif unit:
            self.dim, self.unit = "count", unit  # seats, agents, requests: counted things, comparable only as the same thing
        else:
            self.dim, self.unit = "bare", ""  # a number in a column with no unit in its header: comparable with the same column only

    def comparable(self, other: "Value") -> bool:
        return (self.kind == other.kind == "number" and self.dim == other.dim and self.per == other.per
                and (self.unit == other.unit or self.dim in ("data", "time", "bare") or (self.dim == "count" and stem(self.unit) == stem(other.unit))))

    def show(self) -> str:
        return self.raw


# ---------- the facts of the knowledge base

def _sentence_of(rec: dict, f: dict, answer: str) -> str:
    want = f"{f['label']}: {f['text']}"
    for s in re.split(r"(?<=[.!?])\s+", answer):
        if want in s:
            return s.strip()
    return f"{rec.get('subject') or rec.get('entity')}, {want}." if f["text"] else f"{rec.get('subject') or rec.get('entity')}: {f['header']} is empty."


HEADER_UNIT = re.compile(r"\(\s*(?P<u>[A-Za-z$€£₹%]+)(?:\s+per\s+(?P<p>[A-Za-z]+))?\s*\)")


def unit_in(header: str) -> str:
    """The unit a header gives its bare numbers: "Storage (GB)" -> "GB", "Extra agent (USD per agent)" -> "USD per agent". Only units the reasoning knows."""
    m = HEADER_UNIT.search(header or "")
    if not m:
        return ""
    u = m.group("u")
    if u.lower() in CURRENCY or u.lower() in DATA or u.lower() in TIME or u == "%":
        return u + (f" per {m.group('p')}" if m.group("p") else "")
    return ""


def plain(header: str) -> str:
    """A header as a name: "Monthly price (USD)" -> "Monthly price"."""
    return re.sub(r"\s*\([^)]*\)\s*", " ", header or "").strip() or header


def with_unit(text: str, header: str) -> str:
    return f"{text} {unit_in(header)}" if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", (text or "").strip()) and unit_in(header) else text


class Fact:
    __slots__ = ("entity", "noun", "table", "attr", "header", "value", "entry_id", "source", "sentence", "transposed", "key")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def unit_words(self) -> set[str]:
        u = unit_in(self.header or "") or unit_in(self.attr or "")
        return {stem(w) for w in W.findall(u.lower())}

    @property
    def label_words(self) -> list[str]:
        return [w for w in words(self.attr) if w not in ("per", "of", "a", "the")]


class Model:
    """The facts, entities and tables found in the rows of the knowledge base."""

    def __init__(self, records: list[dict]):
        self.entries = {h["entry_id"]: h for h in records}
        names: dict[str, str] = {}  # entity name (lower) -> noun
        owner: dict[str, tuple] = {}  # entity name (lower) -> the table it is a row of
        for h in records:
            r = h["record"]
            owner.setdefault(r["entity"].lower(), (h.get("source", ""), r.get("table", "")))
            if r.get("noun") and r["entity"]:
                names.setdefault(r["entity"].lower(), r["noun"])
        self.facts: list[Fact] = []
        self.tables: dict[tuple, dict] = {}
        for h in records:
            r = h["record"]
            table = (h.get("source", ""), r.get("table", ""))
            heads = [f["header"].lower() for f in r["fields"] if f["header"]]
            # a sheet whose columns are the names of things named elsewhere ("Starter | Pro | Business") has its rows turned round: its rows are attributes, its columns are the things
            turned = sum(x in names and owner.get(x) != table for x in heads) >= max(2, len(heads) // 2 + 1)  # most columns are rows of another table
            t = self.tables.setdefault(table, {"turned": turned, "noun_terms": set(content(r.get("noun", "")) + content(r.get("table", ""))), "rows": 0})
            t["rows"] += 1
            for f in r["fields"]:
                sentence = _sentence_of(r, f, h.get("answer", ""))
                if turned and f["header"].lower() in names and owner.get(f["header"].lower()) != table:
                    self.facts.append(Fact(entity=f["header"], noun=names[f["header"].lower()], table=table, attr=plain(r["entity"]), header=r["entity"], value=Value(with_unit(f["text"], r["entity"])), entry_id=h["entry_id"],
                                           source=h.get("source", ""), sentence=sentence, transposed=True, key=f["key"]))
                elif not turned:
                    self.facts.append(Fact(entity=r["entity"], noun=r.get("noun") or "", table=table, attr=plain(f["header"]) if f["header"] else f["label"], header=f["header"], value=Value(with_unit(f["text"], f["header"])), entry_id=h["entry_id"],
                                           source=h.get("source", ""), sentence=sentence, transposed=False, key=f["key"]))
        self.names = names
        self.display = {}
        for h in records:
            self.display.setdefault(h["record"]["entity"].lower(), h["record"]["entity"])
        self.by_entity: dict[str, list[Fact]] = {}
        for f in self.facts:
            self.by_entity.setdefault(f.entity.lower(), []).append(f)
        # the things other rows refer to by name (plans: the columns of a turned table, the names in an "available on" cell); else the things called plans, tiers or editions
        refer = {f.entity.lower() for f in self.facts if f.transposed}
        for f in self.facts:
            if not f.transposed and set(words(f.header)) in MEMBER_HEADERS:
                for part in re.split(r",|/|;|\band\b|&|\bor\b", f.value.raw):
                    if part.strip().lower() in self.by_entity:
                        refer.add(part.strip().lower())
        if not refer:
            refer = {e for e, noun in names.items() if set(content(noun)) & {"plan", "tier", "edit", "packag"}}
        self.plans = {e for e in refer if e in self.by_entity}
        self.order = [self.display[e] for e in dict.fromkeys(h["record"]["entity"].lower() for h in records) if e in self.plans]  # in the order the table lists them

    def noun_terms(self, entity: str) -> set[str]:
        out: set[str] = set()
        for f in self.by_entity.get(entity.lower(), []):
            out |= set(content(f.noun)) | self.tables[f.table]["noun_terms"]
        return out


# ---------- reading the question

# groups of words that name the same kind of attribute; a question word counts for any header word of its group
GROUPS = [set(stem(w) for w in g.split()) for g in (
    "price cost fee charge cheap cheapest cheaper expensive priciest pricing priced pricey pay paid billing subscription",
    "storage space capacity size disk gb gigabyte data",
    "limit limits maximum max cap quota allowance allowed ceiling",
    "agent agents seat seats user users member members licence license",
    "time duration long longest fast fastest faster quick quickest slow slowest setup speed",
    "support help assistance",
    "month monthly",
    "year yearly annual annually",
)]
MEMBER_HEADERS = [set(words(p)) for p in ("available on", "available in", "available", "included in", "included on", "included with", "supported plans", "supported on", "plans", "plan", "tier", "tiers",
                                          "edition", "editions", "offered in", "offered on", "part of", "which plans", "plan availability", "availability", "supported by")]
WH = set(words("which what who whom whose how many much does do did is are can could may will would should the a an of in on for to and or with by at than that this these those it its it's has have had "
                "include includes included including support supports supported offer offers offered provide provides provided allow allows allowed come comes get gets there any all every each plans plan "
                "tiers tier please me tell show list give i my we you your our their between difference differences differ differs compare comparison versus vs from available availability included supported listed number per many much use uses using integrate integrates connect connects"))
LIGHT_VERBS = {stem(w) for w in "take need require work start apply provide mean come cost".split()}  # verbs that carry no subject of their own
NOUN_GROUPS = [{stem(w) for w in g.split()} for g in ("plan tier edition package subscription level", "integration connector app", "feature capability option")]
ALL_WORDS = {"all", "every", "any"}
MIN_RE = re.compile(r"\b(cheapest|least expensive|lowest|smallest|fewest|least|minimum|min|shortest|fastest|quickest|cheaper than all)\b", re.I)
MAX_RE = re.compile(r"\b(most expensive|priciest|highest|largest|biggest|greatest|most|maximum|max|longest|slowest|top)\b", re.I)
DIFF_RE = re.compile(r"\b(difference|differences|differ|differs|different|compare|compared|comparison|versus|vs\.?|better than|against|cheaper|more expensive|how much more|how much less|higher than|lower than|bigger than|larger than|smaller than)\b", re.I)
WIDE_DIFF = re.compile(r"\b(difference|differences|differ|differs|different|compare|compared|comparison|versus|vs\.?|better than|against)\b", re.I)
SUM_RE = re.compile(r"\b(total|sum|combined|altogether|in all|average|mean)\b", re.I)
COUNT_RE = re.compile(r"\bhow many\b", re.I)
FILTER_RE = re.compile(r"\b(which|what|list|show|name|how many)\b.{0,40}\b(include|includes|have|has|support|supports|offer|offers|provide|provides|allow|allows|come with|comes with|get|gets|with|available|contain|contains|cover|covers|use|uses|using|work with|works with|integrate with|integrates with|connect to|connects to)\b|"
                       r"\bwhich\b.{0,30}\b(is|are)\b.{0,30}\b(available|included|supported)\b|\b(available|included|supported)\b.{0,12}\b(on|in|with)\b.{0,12}\bwhich\b", re.I)
CAN_RE = re.compile(r"^(can|could|may|does|do|is|are|will|would|am i able|is it possible)\b", re.I)
OPS = [(r"(?:at least|no fewer than|no less than|minimum of|[>≥]=?|min(?:imum)?)", ">="), (r"(?:more than|over|above|greater than|exceeding)", ">"), (r"(?:at most|up to|no more than|maximum of|max(?:imum)? of|[<≤]=?)", "<="),
       (r"(?:less than|under|below|fewer than)", "<")]


def _entities_in(model: Model, question: str) -> list[str]:
    """The names of things (rows) the question mentions, longest first, each once."""
    qw = words(question)
    found = []
    for name in sorted(model.by_entity.keys(), key=lambda n: -len(n)):
        nw = words(name)
        if nw and any(qw[i:i + len(nw)] == nw for i in range(len(qw))):
            if not any(set(nw) <= set(words(x)) for x in found):
                found.append(name)
    first = lambda n: next((i for i in range(len(qw)) if qw[i:i + len(words(n))] == words(n)), 0)  # noqa: E731
    return sorted(found, key=first)  # in the order the question names them


def _expand(ws: list[str]) -> set[str]:
    out = {stem(w) for w in ws}
    for g in GROUPS:
        if out & g:
            out |= g
    return out


def _attribute(model: Model, question: str, facts: list[Fact], skip: set[str]) -> list[tuple[str, list[Fact]]]:
    """The attribute(s) the question is about. A header word that is in the question counts double; one that is only in the same group of meaning ("cost" for "price") counts once. Returns
    the best-scoring attributes with their facts; several when they score alike (the question does not say which)."""
    direct = {w for w in words(question) if w not in WH and w not in skip}
    qw = _expand(list(direct))
    scores: dict[str, tuple[float, list[Fact]]] = {}
    for f in facts:
        lw = set(f.label_words)
        if not lw:
            continue
        hit_direct, hit_group = lw & direct, (lw & qw) - direct
        if not hit_direct and not hit_group:
            continue
        s = 2 * len(hit_direct) + len(hit_group) + 0.5 * len(hit_direct | hit_group) / len(lw)
        cur = scores.setdefault(f.attr.lower(), (s, []))
        cur[1].append(f)
    if not scores:
        return []
    top = max(v[0] for v in scores.values())
    return [(v[1][0].attr, v[1]) for a, v in scores.items() if v[0] >= top - 1e-9]  # named as the table names it


def _quantity(question: str):
    """A number in the question with the word that follows it ("10 seats", "3 Pro plans")."""
    m = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s+([A-Za-z][\w-]*)", question)
    return (float(m.group(1).replace(",", "")), m.group(2)) if m else None


def _plan_like(model: Model, text: str) -> tuple[list[str], bool, str]:
    """The names in a cell such as "Pro, Business and Enterprise", "All plans" or "Growth and above", whether the cell names plans at all (every name in it is the name of a thing that exists
    in the knowledge), and, when an order of the plans was needed to read it, what was assumed."""
    t = text.strip().rstrip(".")
    plans = [n for n in model.order]
    if re.fullmatch(r"(?i)all( plans| tiers| editions)?|every plan|everyone|all customers", t) and plans:
        return plans, True, ""
    up = re.fullmatch(r"(?i)(.+?)\s+(?:and|or|\+)\s+(?:above|higher|up|upwards|bigger)|(.+?)\+", t)
    if up and (up.group(1) or up.group(2)).strip().lower() in {p.lower() for p in plans}:
        start = next(i for i, p in enumerate(plans) if p.lower() == (up.group(1) or up.group(2)).strip().lower())
        return plans[start:], True, f"'{t}' is read using the order in which the table lists the plans"
    parts = [p.strip() for p in re.split(r",|/|;|\band\b|&", t) if p.strip()]
    known = [model.display.get(p.lower(), p) for p in parts if p.lower() in model.plans]
    return known, bool(parts) and len(known) == len(parts), ""


def _cite(res: dict, facts: list[Fact]) -> list[int]:
    """Registers the facts a statement rests on (one reference per entry) and returns their reference numbers."""
    refs = []
    for f in facts:
        if f.entry_id not in res["entries"]:
            res["entries"][f.entry_id] = {"sentences": []}
        if f.sentence not in res["entries"][f.entry_id]["sentences"]:
            res["entries"][f.entry_id]["sentences"].append(f.sentence)
        refs.append(f.entry_id)
    return list(dict.fromkeys(refs))


def show_cell(f: Fact) -> str:
    return f.value.show() or "empty"


def _gap(res: dict, text: str, facts: list[Fact]) -> None:
    res["gaps"].append((text, _cite(res, facts)))


def _assume(res: dict, text: str) -> None:
    if text not in [t for t, _ in res["assumptions"]]:
        res["assumptions"].append((text, [e for _, refs in res["lines"][:1] for e in refs]))


def _result(op: str) -> dict:
    return {"operation": op, "lines": [], "entries": {}, "assumptions": [], "gaps": [], "values": [], "numbers": []}  # `numbers`: every figure the operation itself produced (a total, a difference, a count)


# ---------- the operations

def _comparable_set(facts: list[Fact]):
    """The facts with a number, grouped by what they can be compared with; the ones that are not numbers, to be named."""
    numeric = [f for f in facts if f.value.kind in ("number", "unlimited")]
    others = [f for f in facts if f.value.kind not in ("number", "unlimited")]
    return numeric, others


def _one_per_entity(facts: list[Fact]) -> tuple[list[Fact], list[str]]:
    """One fact per thing; a thing that two sources give different values for is left out and named (it is a conflict, not something to pick from)."""
    by: dict[str, list[Fact]] = {}
    for f in facts:
        by.setdefault(f.entity.lower(), []).append(f)
    keep, clash = [], []
    for _k, fs in by.items():
        if len({f.value.raw.lower() for f in fs}) > 1:
            clash.append(fs[0].entity)
        else:
            keep.append(fs[0])
    return keep, clash


def _of_kind(model: Model, f: Fact, noun_words: set[str]) -> bool:
    return not noun_words or bool(noun_words & (set(content(f.noun)) | model.tables[f.table]["noun_terms"]))


def superlative(model: Model, question: str, entities: set[str] | None, noun_words: set[str] = frozenset()) -> dict | None:
    want_min, want_max = bool(MIN_RE.search(question)), bool(MAX_RE.search(question))
    if want_min == want_max:
        return None
    pool = [f for f in model.facts if (entities is None or f.entity.lower() in entities) and _of_kind(model, f, noun_words)]
    attrs = _attribute(model, question, pool, set())
    if not attrs:
        return None
    res = _result("minimum" if want_min else "maximum")
    for attr, facts in attrs:
        facts, clash = _one_per_entity(facts)
        numeric, others = _comparable_set(facts)
        if not numeric:
            continue
        base = max(numeric, key=lambda f: sum(f.value.comparable(g.value) for g in numeric))
        group = [f for f in numeric if f.value.comparable(base.value) or f.value.kind == "unlimited"]
        odd = [f for f in numeric if f not in group]
        finite = [f for f in group if f.value.kind == "number"]
        if want_max and any(f.value.kind == "unlimited" for f in group):
            best = [f for f in group if f.value.kind == "unlimited"]
        elif finite:
            target = (min if want_min else max)(f.value.num for f in finite)
            best = [f for f in finite if f.value.num == target]
        else:
            continue
        shown = attr
        names = " and ".join(f.entity for f in best)
        word = "lowest" if want_min else "highest"
        line = f"{names} {'has' if len(best) == 1 else 'have'} the {word} {shown}: {best[0].value.show()}."
        res["lines"].append((line, _cite(res, best)))
        cmp_ = ", ".join(f"{f.entity} {f.value.show()}" for f in sorted(group, key=lambda f: (f.value.num if f.value.num is not None else 1e18)))
        res["lines"].append((f"Compared ({shown}): {cmp_}.", _cite(res, group)))
        if odd:
            _gap(res, f"not compared, because the unit or what it counts is different: {', '.join(f'{f.entity} ({f.value.show()})' for f in odd)}", odd)
        if others:
            _gap(res, f"no value to compare for {shown}: {', '.join(f'{f.entity} ({show_cell(f)})' for f in others)}", others)
        if clash:
            _gap(res, f"the sources give different values for: {', '.join(clash)}", [f for f in group if f.entity in clash] or best)
        res["values"].append({"attribute": attr, "winner": [f.entity for f in best], "compared": [{"entity": f.entity, "value": f.value.show()} for f in group]})
    return res if res["lines"] else None


def _unexplained(model: Model, question: str, explained: set[str]) -> list[str]:
    """Content words of the question that nothing in the answer accounts for ("change" in "when will the price change"): a lookup that ignores them would answer another question."""
    skip = explained | _expand(list(explained)) | _expand(["price"]) | LIGHT_VERBS  # (asking for a price or an amount says nothing the attribute does not)
    return [w for w in content(question) if w not in skip and w not in WH and w not in ALL_WORDS and not w.isdigit()]


def lookup(model: Model, question: str, named: list[str]) -> dict | None:
    if not named or len(named) > 1 or DIFF_RE.search(question) or MIN_RE.search(question) or MAX_RE.search(question):
        return None
    facts = model.by_entity.get(named[0].lower(), [])
    skip = set(words(named[0]))
    attrs = _attribute(model, question, facts, skip)
    if not attrs or len(attrs) > 2:
        return None
    labelled = {w for _, fs in attrs for f in fs for w in f.label_words} | skip | {w for n in model.by_entity[named[0].lower()] for w in content(n.noun) + list(model.tables[n.table]["noun_terms"])}
    if _unexplained(model, question, labelled | {w for w in content(question) if w in _expand(list(labelled))}):
        return None
    res = _result("lookup")
    for _attr, fs in attrs:
        for f in fs:
            if f.value.kind == "missing":
                _gap(res, f"the table has the column '{f.header}' for {f.entity}, and nothing in it", [f])
            else:
                res["lines"].append((f.sentence, _cite(res, [f])))
                res["values"].append({"entity": f.entity, "attribute": f.attr, "value": f.value.show()})
    return res if (res["lines"] or res["gaps"]) else None


def _feature_sources(model: Model, wanted: list[str]):
    """Where the knowledge says which things have a feature: (rows named for the feature with a column that names plans, rows whose own lists mention the feature, what could not be used)."""
    via: dict[str, tuple[list[str], Fact, str]] = {}
    unusable: list[tuple[str, Fact]] = []
    for e, fs in model.by_entity.items():
        if not set(wanted) <= set(content(e)):
            continue
        for f in fs:
            if f.transposed or f.value.kind == "missing":
                continue
            heads = set(words(f.header))
            if any(h == heads or h <= heads for h in MEMBER_HEADERS):
                got, plans, note = _plan_like(model, f.value.raw)
                if plans:
                    via[f.entity] = (got, f, note)
                else:
                    unusable.append((f"the column '{f.header}' for {f.entity} holds '{f.value.raw}', which does not name plans, so it was not read as plans", f))
    by_list: dict[str, Fact] = {}
    for f in model.facts:
        if f.transposed or f.value.kind == "missing" or not f.value.raw or f.entity.lower() not in model.plans:
            continue
        if set(wanted) <= set(content(f.value.raw)):
            by_list.setdefault(f.entity, f)
    return via, by_list, unusable


def membership(model: Model, question: str, wanted: list[str]) -> dict | None:
    """Which things have a feature. Two ways a table can say it, and both are read: rows that list the feature among what they include ("Pro | includes | API access and priority
    support"), and a row named for the feature with a column that says which plans it is on ("Slack | available on | Pro, Business and Enterprise"). A column named like "available on"
    is read as naming plans only if what is in it are names of plans; otherwise the answer says what the column holds."""
    res = _result("filter")
    if not wanted:
        return None
    via, by_list, unusable = _feature_sources(model, wanted)
    for text, f in unusable:
        _gap(res, text, [f])
    names: list[str] = []
    for _ent, (got, f, note) in via.items():
        res["lines"].append((f"{f.entity} is listed under '{f.header}': {f.value.raw}.", _cite(res, [f])))
        if note:
            res["assumptions"].append((note, [f.entry_id]))
        names += got
    for ent, f in by_list.items():
        res["lines"].append((f.sentence, _cite(res, [f])))
        names.append(ent)
    names = list(dict.fromkeys(names))
    if not via and not by_list:
        return res if res["gaps"] else None
    if via and by_list and {x.lower() for v in via.values() for x in v[0]} != {x.lower() for x in by_list}:
        _gap(res, "the rows that name the feature and the rows that list it give different plans; both are shown", [by_list[e] for e in by_list])
    order = {p.lower(): i for i, p in enumerate(model.order)}
    names.sort(key=lambda n: order.get(n.lower(), 99))
    res["lines"].insert(0, (f"Listed with it: {', '.join(names)}.", list(dict.fromkeys(e for _, refs in res["lines"] for e in refs))[:3]))
    res["values"].append({"feature": " ".join(wanted), "things": names})
    return res


def membership_check(model: Model, question: str, named: list[str], wanted: list[str]) -> dict | None:
    """Does this plan have that feature: yes if the table lists it, no if the table lists the feature for other plans or in a column where this plan's value lacks it, and nothing
    (the ordinary answer, or none) when the knowledge never mentions the feature at all: not being listed is only evidence when the thing could have been listed."""
    plans = [n for n in named if n.lower() in model.plans]
    if len(plans) != 1 or not wanted or not CAN_RE.search(question.strip()):
        return None
    plan = plans[0]
    via, by_list, unusable = _feature_sources(model, wanted)
    res = _result("membership check")
    for ent, (got, f, note) in via.items():
        has = plan.lower() in {g.lower() for g in got}
        if has:
            res["lines"].append((f"Yes: {f.entity} is listed under '{f.header}': {f.value.raw}.", _cite(res, [f])))
        else:
            res["lines"].append((f"No: {f.entity} is listed under '{f.header}': {f.value.raw}, which does not include {model.display.get(plan.lower(), plan)}.", _cite(res, [f])))
        if note:
            res["assumptions"].append((note, [f.entry_id]))
        res["values"].append({"plan": plan, "feature": ent, "has": has})
        return res
    if unusable:
        for text, f in unusable:
            _gap(res, text, [f])
        return res
    mine = model.by_entity.get(plan.lower(), [])
    for f in mine:  # the plan's own list holds the feature
        if f.value.raw and set(wanted) <= set(content(f.value.raw)) and not f.transposed:
            res["lines"].append((f"Yes: {f.sentence}", _cite(res, [f])))
            res["values"].append({"plan": plan, "feature": " ".join(wanted), "has": True})
            return res
    for f in mine:  # a column where other plans list it: this plan's value says what it has instead
        if f.transposed or not f.value.raw:
            continue
        others = [g for g in model.facts if g.key == f.key and g.table == f.table and g.entity != f.entity and not g.transposed and set(wanted) <= set(content(g.value.raw))]
        if others:
            res["lines"].append((f"No: {f.sentence[:-1]}; {' '.join(wanted)} is not among them (it is listed for {others[0].entity}).", _cite(res, [f, others[0]])))
            res["values"].append({"plan": plan, "feature": " ".join(wanted), "has": False})
            return res
    return None


def count_or_sum(model: Model, question: str, entities: set[str] | None, noun_words: set[str]) -> dict | None:
    if COUNT_RE.search(question) and not SUM_RE.search(question):
        rows = {f.entity for f in model.facts if (not noun_words or noun_words & (set(content(f.noun)) | model.tables[f.table]["noun_terms"])) and not f.transposed}
        if not rows or not noun_words:
            return None
        ents = sorted(rows)
        res = _result("count")
        facts = []
        for e in ents:
            own = [f for f in model.by_entity.get(e.lower(), []) if not f.transposed and _of_kind(model, f, noun_words)]
            facts += own[:1] or model.by_entity.get(e.lower(), [])[:1]
        res["lines"].append((f"There are {len(ents)} listed: {', '.join(ents)}.", _cite(res, facts)))
        res["numbers"].append(str(len(ents)))
        res["values"].append({"count": len(ents), "items": ents})
        return res
    if SUM_RE.search(question):
        avg = bool(re.search(r"average|mean", question, re.I))
        pool = [f for f in model.facts if entities is None or f.entity.lower() in entities]
        attrs = _attribute(model, question, pool, set())
        if len(attrs) != 1:
            return None
        attr, facts = attrs[0]
        facts, clash = _one_per_entity(facts)
        numeric, others = _comparable_set([f for f in facts if (not noun_words or noun_words & (set(content(f.noun)) | model.tables[f.table]["noun_terms"]))])
        numeric = [f for f in numeric if f.value.kind == "number"]
        if not numeric:
            return None
        base = max(numeric, key=lambda f: sum(f.value.comparable(g.value) for g in numeric))
        group = [f for f in numeric if f.value.comparable(base.value)]
        total = sum(f.value.num for f in group)
        res = _result("average" if avg else "sum")
        unit = f" {base.value.unit}" if base.value.unit and base.value.dim not in ("bare",) else ""
        if base.value.dim == "data":
            unit = " GB"
        val = total / len(group) if avg else total
        res["lines"].append((f"The {'average' if avg else 'total'} {attr} over {len(group)} listed ({', '.join(f.entity for f in group)}) is {val:g}{unit}.", _cite(res, group)))
        omitted = [f for f in numeric if f not in group] + others
        if omitted:
            _gap(res, f"left out, not comparable or no value: {', '.join(f'{f.entity} ({show_cell(f)})' for f in omitted)}", omitted)
        if base.value.dim == "data":
            _assume(res, "sizes were converted to GB (1 TB = 1000 GB)")
        res["values"].append({"attribute": attr, "result": val, "of": [f.entity for f in group]})
        res["numbers"] += [f"{val:g}", str(len(group))]
        return res
    return None


def difference(model: Model, question: str, named: list[str]) -> dict | None:
    if len(named) < 2 or not DIFF_RE.search(question):
        return None
    a, b = named[0], named[1]
    fa, fb = {f.attr.lower(): f for f in model.by_entity.get(a.lower(), [])}, {f.attr.lower(): f for f in model.by_entity.get(b.lower(), [])}
    if not fa or not fb:
        return None
    res = _result("difference")
    same, only = [], []
    found = _attribute(model, question, model.by_entity.get(a.lower(), []) + model.by_entity.get(b.lower(), []), set(words(a)) | set(words(b)))
    keep = {x[0].lower() for x in found} or None  # "how much is A per month compared to B": only what was asked about
    if keep is None and not WIDE_DIFF.search(question):
        return None
    for attr in dict.fromkeys([*fa, *fb]):
        if keep is not None and attr not in keep:
            continue
        x, y = fa.get(attr), fb.get(attr)
        if x and y:
            if x.value.raw.lower() == y.value.raw.lower():
                same.append((attr, x, y))
                continue
            q_ = lambda f: (f'"{f.value.show()}"' if f.value.kind == "text" else f.value.show()) if f.value.show() else "nothing listed"  # noqa: E731
            line = f"{x.attr}: {x.entity} {q_(x)}; {y.entity} {q_(y)}"
            if x.value.comparable(y.value) and x.value.num != y.value.num:
                d = abs(x.value.num - y.value.num)
                bigger = x.entity if x.value.num > y.value.num else y.entity
                u = f" {x.value.unit}" if x.value.dim in ("money", "count", "percent") and x.value.unit else ""
                if x.value.dim == "data":  # worked out in GB, said in the unit the table uses
                    unit = (re.search(r"([KMGTP]B)\s*$", x.value.raw, re.I) or re.search(r"([KMGTP]B)", x.value.raw, re.I) or [None, "GB"])[1].lower()
                    d, u = d / DATA.get(unit, 1.0), f" {unit.upper()}"
                line += f" ({bigger} is higher by {d:g}{u})"
                res["numbers"].append(f"{d:g}")
            res["lines"].append((line + ".", _cite(res, [x, y])))
        else:
            only.append(x or y)
    if same:
        res["lines"].append((f"The same for both: {', '.join(f'{x.attr} ({x.value.show()})' for _, x, _y in same)}.", _cite(res, [x for _, x, _y in same] + [y for _, _x, y in same])))
    if only:
        _gap(res, f"listed for one of them only: {', '.join(f'{f.attr} ({f.entity})' for f in only)}", only)
    res["values"].append({"between": [a, b]})
    return res if res["lines"] else None


def limit_check(model: Model, question: str, named: list[str]) -> dict | None:
    """Can a thing have N of something: compared with the figure its row lists (read as the amount it includes; the answer says so)."""
    q = _quantity(question)
    if not q or not CAN_RE.search(question.strip()) or len(named) != 1:
        return None
    n, noun = q
    asked = f"{n:g} {noun}"
    facts = sorted([f for f in model.by_entity.get(named[0].lower(), []) if f.value.kind in ("number", "unlimited") and (stem(noun) in f.label_words or stem(noun) in f.unit_words)], key=lambda f: len(f.label_words))
    if not facts or (len(facts) > 1 and len(facts[0].label_words) == len(facts[1].label_words)):
        return None
    f = facts[0]
    if f.value.dim == "data" and noun.lower() in DATA:
        n = n * DATA[noun.lower()]  # the table gives sizes in GB, the question in MB
    res = _result("limit check")
    if f.value.kind == "unlimited":
        res["lines"].append((f"Yes: {f.entity} lists {f.attr} as {f.value.show()}.", _cite(res, [f])))
    else:
        ok = n <= f.value.num + 1e-12
        res["lines"].append((f"{'Yes' if ok else 'No'}: {f.entity} lists {f.attr}: {f.value.show()}, and you asked about {asked}.", _cite(res, [f])))
        res["values"].append({"asked": asked, "listed": f.value.show(), "allowed": ok})
    _assume(res, f"the listed {f.attr} is read as the amount {f.entity} includes; nothing says whether more can be added")
    return res


def threshold_filter(model: Model, question: str, noun_words: set[str]) -> dict | None:
    """Which things have at least / at most / more than N of an attribute."""
    m = None
    for pat, op in OPS:  # noqa: B007  (op is the operator of the pattern that matched, used after the loop)
        m = re.search(rf"\b({pat})\s*(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z][\w-]*)?", question, re.I)
        if m:
            break
    if not m or not re.search(r"\b(which|what|list)\b", question, re.I):
        return None
    n, unit = float(m.group(2).replace(",", "")), m.group(3) or ""
    pool = [f for f in model.facts if (not noun_words or noun_words & (set(content(f.noun)) | model.tables[f.table]["noun_terms"]))]
    attrs = [a for a in _attribute(model, question + " " + unit, pool, set()) if len(a[1]) > 1]
    if len(attrs) != 1:
        return None
    attr, facts = attrs[0]
    facts, clash = _one_per_entity(facts)
    if unit.lower() in DATA:
        n = n * DATA[unit.lower()]  # asked in MB, compared in GB like the table's values
    elif unit.lower() in TIME:
        n = n * TIME[unit.lower()]
    cmp = {">=": lambda x: x >= n, ">": lambda x: x > n, "<=": lambda x: x <= n, "<": lambda x: x < n}[op]
    got = [f for f in facts if f.value.kind == "unlimited" and op in (">=", ">") or (f.value.kind == "number" and cmp(f.value.num))]
    unknown = [f for f in facts if f.value.kind not in ("number", "unlimited")]
    res = _result("threshold")
    sign = {">=": "at least", ">": "more than", "<=": "at most", "<": "fewer than"}[op]
    if got:
        res["lines"].append((f"{', '.join(f.entity for f in got)} {'has' if len(got) == 1 else 'have'} {sign} {m.group(2)} {unit} {attr}: {', '.join(f'{f.entity} {f.value.show()}' for f in got)}.".replace("  ", " "), _cite(res, got)))
    else:
        res["lines"].append((f"None of the listed has {sign} {m.group(2)} {unit} {attr}.".replace("  ", " "), _cite(res, facts)))
    if unknown:
        _gap(res, f"no value to compare for: {', '.join(f'{f.entity} ({show_cell(f)})' for f in unknown)}", unknown)
    res["values"].append({"attribute": attr, "threshold": n, "operator": op, "matches": [f.entity for f in got]})
    return res


def compound_filter(model: Model, part: str, qnouns: set[str], plan_words: set[str]) -> dict | None:
    """Conditions of different kinds in one question ("more than 5 agents and WhatsApp"): each is established on its own and the answer is the things that satisfy ALL of them."""
    m = re.search(r"\b(?:include|includes|have|has|support|supports|offer|offers|provide|provides|allow|allows|come with|comes with|contain|contains|with)\b\s+(?:both\s+)?(?P<rest>.+)$", part, re.I)
    if not m or not qnouns:
        return None
    pieces = [p_.strip() for p_ in re.split(r"\s*(?:,|&|\band\b|\bas well as\b)\s*", m.group("rest")) if p_.strip()]
    if len(pieces) < 2 or not any(any(re.search(pat, p_, re.I) for pat, _ in OPS) for p_ in pieces):
        return None
    res, name_sets = _result("filter"), []
    for p_ in pieces:
        if any(re.search(pat, p_, re.I) for pat, _ in OPS):
            r = threshold_filter(model, f"Which plans have {p_}", qnouns)
            names = {x.lower() for x in (r["values"][0]["matches"] if r and r["values"] else [])}
        else:
            words_ = [w for w in content(p_) if w not in qnouns and w not in plan_words and w not in WH]
            r = membership(model, part, words_) if words_ else None
            names = {x.lower() for x in (r["values"][0]["things"] if r and r["values"] else [])}
        if not r:
            return None
        name_sets.append(names)
        for text, refs in r["lines"]:
            if not text.startswith(("Listed with it", "None of")):
                res["lines"].append((text, refs))
        res["entries"].update(r["entries"])
        res["gaps"] += r["gaps"]
        res["assumptions"] += r["assumptions"]
    common = set.intersection(*name_sets)
    order = {p.lower(): i for i, p in enumerate(model.order)}
    shown = sorted(common, key=lambda n: order.get(n, 99))
    res["lines"].insert(0, (f"Meeting all of them: {', '.join(model.display.get(n, n) for n in shown)}." if shown else "Nothing meets all of them.", list(dict.fromkeys(e for _, rs in res["lines"] for e in rs))[:4]))
    res["values"].append({"conditions": pieces, "things": [model.display.get(n, n) for n in shown]})
    return res


def arithmetic(model: Model, question: str, named: list[str]) -> dict | None:
    """N times a listed price: "3 Team plans", "12 extra agents" when a price per agent is listed, "6 months" of a monthly price. A price that is not a number ("Contact us") is said
    to be the reason nothing could be worked out."""
    if not re.search(r"\b(cost|price|pay|total|charge|bill|how much)\b", question, re.I):
        return None
    m = re.search(r"\b(\d[\d,]*(?:\.\d+)?)\s+((?:[A-Za-z][\w-]*\s?){1,3})", question)
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    nouns = [stem(w) for w in W.findall(m.group(2).lower())][:3]
    per_cands = []
    for f in model.facts:
        if f.value.kind != "number" or f.value.dim != "money" or not f.value.per:
            continue
        if stem(f.value.per) in nouns and (not named or f.entity.lower() in {x.lower() for x in named}):
            per_cands.append(f)
    res = _result("multiplication")
    if len(per_cands) == 1:
        chosen = per_cands
    elif len(named) == 1:
        mine = model.by_entity.get(named[0].lower(), [])
        priced = [f for f in mine if set(f.label_words) & _expand(["price"]) and f.value.dim in ("money", "") and f.value.kind in ("number", "text", "missing") and not f.value.per]
        qw = set(words(question))
        if qw & {stem("month"), "monthly"}:  # the question says which period: only that column
            priced = [f for f in priced if {"monthly", "month"} & set(f.label_words)] or priced
        elif qw & {stem("year"), "yearly", "annual", "annually"}:
            priced = [f for f in priced if {"yearly", "annual", "annually", "year"} & set(f.label_words)] or priced
        if not (set(nouns) & ({stem(w) for w in W.findall(named[0].lower())} | {"plan", "licenc", "license", "subscription", "account", "seat", stem("month"), stem("year")} | set(content(model.by_entity[named[0].lower()][0].noun)))):
            return None
        chosen = priced
    else:
        return None
    if not chosen:
        return None
    for f in chosen:  # one line for each column the question could mean (monthly and annual price, when it does not say)
        if f.value.kind != "number":
            _gap(res, f"I cannot calculate this: the listed {f.attr} for {f.entity} is '{f.value.show() or 'empty'}', not a number", [f])
            continue
        total = n * f.value.num
        res["lines"].append((f"{n:g} × {f.value.show()} = {total:g} {f.value.unit} ({f.entity}: {f.attr} {f.value.show()}).", _cite(res, [f])))
        _assume(res, f"the listed {f.attr} is the price of one {f.value.per or ('month' if 'month' in nouns else (f.noun or 'unit'))}; taxes, discounts and any minimum are not in the figures")
        res["values"].append({"quantity": n, "unit_price": f.value.show(), "result": total, "currency": f.value.unit, "attribute": f.attr})
        res["numbers"] += [f"{total:g}", f"{n:g}"]
    return res if (res["lines"] or res["gaps"]) else None


def solve(model: Model | None, part: str) -> dict | None:
    """The structured answer to one part of a question, or None when this is not a question about the rows of a table (or they cannot say). Each result: operation, lines [(sentence,
    [entry ids])], entries {id: sentences used}, assumptions, gaps, values."""
    if model is None or not model.facts:
        return None
    part = part.strip().rstrip("?")
    named = _entities_in(model, part)
    have = {w for f in model.facts for w in (set(content(f.noun)) | model.tables[f.table]["noun_terms"])}
    qnouns = {w for w in content(part)} & have
    for group in NOUN_GROUPS:  # "which tiers" asks about the things the table calls plans
        if group & set(content(part)) and group & have:
            qnouns |= group & have
    ents = {n.lower() for n in named} or None
    # order matters: the most specific shapes first
    plan_words = {w for n in named if n.lower() in model.plans for w in words(n)}
    feature_words = [w for w in content(part) if w not in qnouns and w not in plan_words and w not in WH and w not in _expand(["price"]) and w not in ALL_WORDS]
    steps = [lambda: difference(model, part, named), lambda: arithmetic(model, part, named), lambda: limit_check(model, part, named), lambda: membership_check(model, part, named, feature_words),
             lambda: compound_filter(model, part, qnouns, plan_words), lambda: threshold_filter(model, part, qnouns), lambda: superlative(model, part, ents if named else None, qnouns), lambda: lookup(model, part, named)]
    for fn in steps:
        r = fn()
        if r:
            return r
    if FILTER_RE.search(part) and qnouns and not COUNT_RE.search(part):  # "which plans support both WhatsApp and SSO": every condition, then the plans that have all of them
        m = re.search(r"\b(?:include|includes|have|has|support|supports|offer|offers|provide|provides|allow|allows|come with|comes with|contain|contains|with)\b\s+(?:both\s+|all of\s+)?(?P<rest>.+)$", part, re.I)
        pieces = [[w for w in content(p_) if w not in qnouns and w not in plan_words and w not in WH] for p_ in re.split(r"\s*(?:,|&|\band\b|\bas well as\b)\s*", m.group("rest"))] if m else []
        pieces = [p_ for p_ in pieces if p_]
        if len(pieces) >= 2:
            subs = [membership(model, part, p_) for p_ in pieces]
            if all(subs):
                names = [set(x.lower() for x in r["values"][0]["things"]) if r["values"] else set() for r in subs]
                common = set.intersection(*names)
                res = _result("filter")
                for r in subs:
                    for text, refs in r["lines"][1:]:
                        res["lines"].append((text, refs))
                    res["entries"].update(r["entries"])
                    res["gaps"] += r["gaps"]
                    res["assumptions"] += r["assumptions"]
                order = {p.lower(): i for i, p in enumerate(model.order)}
                shown = sorted(common, key=lambda n: order.get(n, 99))
                refs = list(dict.fromkeys(e for r in subs for _, rs in r["lines"] for e in rs))[:4]
                res["lines"].insert(0, (f"Listed with all of them: {', '.join(model.display.get(n, n) for n in shown)}." if shown else "No plan is listed with all of them.", refs))
                res["values"].append({"features": [" ".join(p_) for p_ in pieces], "things": [model.display.get(n, n) for n in shown]})
                return res
    if FILTER_RE.search(part) and qnouns and not (COUNT_RE.search(part) and not feature_words):
        feature = feature_words or [w for n in named for w in content(n)]
        if feature:
            r = membership(model, part, feature)
            if r:
                if COUNT_RE.search(part):  # "how many plans include it": the number of the things that do
                    names = r["values"][0]["things"] if r["values"] else []
                    r["lines"].insert(0, (f"{len(names)} of them: {', '.join(names)}.", list(r["entries"])[:3]))
                    r["numbers"].append(str(len(names)))
                return r
    r = count_or_sum(model, part, ents, qnouns)
    if r:
        return r
    return None


def render(res: dict) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """The result as [(sentence, [entry ids])]: the statements, then what was left out or assumed, each with the entries it concerns; and the entries in the order of their references."""
    notes = [(f"Not used: {t}.", refs) for t, refs in res["gaps"]] + [(f"Reading: {t}.", refs) for t, refs in res["assumptions"]]
    lines = list(res["lines"]) + notes
    order = list(dict.fromkeys(e for _, refs in lines for e in refs))
    for e in res["entries"]:
        if e not in order:
            order.append(e)
    return lines, order
