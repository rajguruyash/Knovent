"""Dates INSIDE a sentence. A source can say when it stops being valid (validity.py); a sentence can too: "Until 30 June 2026 support is open Monday to Friday. From 1 July 2026 it is open
Monday to Saturday." Both are true statements about different times, so they do not contradict each other, and which one answers a question depends on the day the question is about.

`in_force(sentence, day)` is False only when the sentence says in so many words that it does not hold on `day`; a sentence with no dates (or dates this module does not read) is None, which is
"no reason to leave it out". Only full dates are read (2026-07-01, 1 July 2026, July 1, 2026): "from July" is not a day, and nothing is guessed. Standard library only.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

MONTHS = {m: i for i, m in enumerate("january february march april may june july august september october november december".split(), 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
_M = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE = re.compile(rf"(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})|(?P<d1>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<m1>{_M})\.?,?\s+(?P<y1>\d{{4}})|(?P<m2>{_M})\.?\s+(?P<d2>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{{4}})", re.I)
FROM = re.compile(r"(from|starting|starts?|beginning|effective|as of|since|after|commencing|with effect from|on and after)\s*(?:on\s+)?$", re.I)
UNTIL = re.compile(r"(until|till|through|up to|up until|ending|ends?|expires?|valid until|no later than|by|before)\s*(?:on\s+)?$", re.I)
BEFORE = re.compile(r"\b(before|prior to)\s*$", re.I)


def _day(m: re.Match) -> date | None:
    try:
        if m.group("iso"):
            return date.fromisoformat(m.group("iso"))
        if m.group("d1"):
            return date(int(m.group("y1")), MONTHS[m.group("m1").lower()], int(m.group("d1")))
        return date(int(m.group("y2")), MONTHS[m.group("m2").lower()], int(m.group("d2")))
    except (ValueError, KeyError):
        return None


def dates_in(text: str) -> list[date]:
    return [d for m in DATE.finditer(text) if (d := _day(m))]


def reference_day(question: str, today: date) -> date:
    """The day a question is about: a full date written in it (the latest, "from 1 July 2026"), else today."""
    found = dates_in(question)
    return max(found) if found else today


def bounds(sentence: str) -> tuple[date | None, date | None]:
    """(first day, last day) the sentence says it holds, each None when it names none."""
    start = end = None
    for m in DATE.finditer(sentence):
        d = _day(m)
        if not d:
            continue
        before = sentence[max(0, m.start() - 24):m.start()]
        if BEFORE.search(before):
            end = d - timedelta(days=1)
        elif UNTIL.search(before):
            end = d
        elif FROM.search(before):
            start = d
    return start, end


def in_force(sentence: str, day: date) -> bool | None:
    start, end = bounds(sentence)
    if start is None and end is None:
        return None
    return not ((start and day < start) or (end and day > end))
