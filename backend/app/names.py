"""Pure name matching. Speech cannot convey spelling, so names are compared by a small
phonetic key (Imraan == Imran, Quraishi == Qureshi) instead of by raw characters."""
from __future__ import annotations

import re

HONORIFICS = {"ji", "mr", "mrs", "ms", "shri", "smt", "sir", "madam", "saab", "sahab", "sahib", "dr"}


def key(token: str) -> str:
    t = re.sub(r"[^a-z]", "", token.lower())
    for a, b in (("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("ai", "e")):
        t = t.replace(a, b)
    return re.sub(r"(.)\1+", r"\1", t)


def keys(name: str) -> list[str]:
    toks = re.findall(r"[A-Za-z]+", name.replace("'s", ""))
    return [k for k in (key(t) for t in toks if t.lower() not in HONORIFICS) if k]


def _tok_match(q: str, p: str) -> bool:
    if q == p:
        return True
    # a spoken initial ("R") may stand for a longer stored token, never the reverse
    return len(q) == 1 and p.startswith(q)


def match(query: str, patient_name: str) -> str | None:
    """'exact' (same multiset of tokens), 'loose' (every query token found), or None."""
    q, p = keys(query), keys(patient_name)
    if not q or not p:
        return None
    if sorted(q) == sorted(p):
        return "exact"
    pool = list(p)
    for tok in q:
        hit = next((x for x in pool if _tok_match(tok, x)), None)
        if hit is None:
            return None
        pool.remove(hit)
    return "loose"


def best(query: str, names: list[tuple[str, str]]) -> list[str]:
    """names = [(id, name)]. Exact matches win outright; otherwise all loose matches."""
    hits = [(i, match(query, n)) for i, n in names]
    exact = [i for i, m in hits if m == "exact"]
    return exact or [i for i, m in hits if m == "loose"]


def same_person(a: str, b: str) -> bool:
    ka, kb = set(keys(a)), set(keys(b))
    return bool(ka and kb and (ka <= kb or kb <= ka))


def norm_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d
