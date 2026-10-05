"""Honesty guard. Every time, date and id in a reply must trace back to a tool result, a tool
argument, or something the caller said. Anything else is a fact the agent made up."""
from __future__ import annotations

import datetime as dt
import json
import re

MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
TIME_RE = re.compile(r"\b\d{2}:\d{2}\b")
ID_RE = re.compile(r"\b(?:ap|pt|ho)_\d{4}\b|\bdr_[a-z]+\b")
DATE_RE = re.compile(r"\b(\d{1,2}) (" + "|".join(MONTH_NAMES) + r")\b")


def human_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.strftime('%A')}, {d.day} {MONTH_NAMES[d.month - 1]}"


def ungrounded(reply: str, sources: list) -> list[str]:
    """Return the facts in `reply` that appear in none of `sources` (JSON-serialisable objects)."""
    blob = json.dumps(sources, default=str)
    allowed_dates = set()
    for iso in re.findall(r"\d{4}-\d{2}-\d{2}", blob):
        try:
            d = dt.date.fromisoformat(iso)
            allowed_dates.add((d.day, MONTH_NAMES[d.month - 1]))
        except ValueError:
            pass
    bad = [t for t in TIME_RE.findall(reply) if t not in blob]
    bad += [i for i in ID_RE.findall(reply) if i not in blob]
    bad += [f"{m[0]} {m[1]}" for m in DATE_RE.findall(reply) if (int(m[0]), m[1]) not in allowed_dates]
    return bad
