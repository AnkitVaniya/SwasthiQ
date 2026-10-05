"""Language understanding as pure functions: text in, facts out. No tools, no database.
The agent never trusts these facts to be *safe*; it only uses them to decide what to ask the tools."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Optional

from . import names

# ======================================================================================
# 1. Safety lexicons. Run on the RAW turn, before anything else, about anyone.
# ======================================================================================
W = r"(?:\w+\W+)"  # one word plus its separator
RED_FLAGS = [
    rf"\b(?:seene|seena|sine|chhati|chaati|chest)\W+{W}{{0,3}}?(?:dard|pain|takleef|dikkat|jalan|bhaari|bhari|dabav|pressure|jakad|tight)",
    r"\bdil\W+(?:ka\W+)?(?:daura|dard|ghabra)", r"\bheart\s*attack\b",
    rf"\bsaans\W+{W}{{0,4}}?(?:phool|phul|nahi\W+aa|nahin\W+aa|ruk|takleef|dikkat|mushkil|ukhad|tez)",
    r"\bsaans\s+lene\s+mein\b", r"\bbreathless", r"\b(?:can'?t|cannot|difficulty|trouble)\s+(?:breathe|breathing)\b",
    r"\bshort\s+of\s+breath\b", r"\bdum\s+ghut", r"\bdam\s+ghut",
    r"\bbe?hosh\b", r"\bunconscious\b", r"\bunresponsive\b", r"\bresponse\s+nahi\b", r"\bhosh\s+(?:nahi|nahin|mein\s+nahi)",
    r"\bcollapse", r"\bgir\s+(?:gaya|gayi|gaye)\b",
    r"\bmirgi\b", r"\bseizure", r"\bconvuls", r"\bdaura\s+pad", r"\bjhatke\s+aa",
    rf"\bkhoon\W+{W}{{0,2}}?(?:nahi\W+ruk|beh|bahut|zyada)", rf"\b(?:bahut|zyada)\W+{W}{{0,2}}?khoon\b",
    r"\bheavy\s+bleeding\b", r"\bbleeding\s+(?:a\s+lot|heavily)\b",
    r"\bchehra\s+tedha\b", r"\bface\s+droop", r"\bzubaan\s+lad", r"\bstroke\b", r"\blakwa\b", r"\bparalys",
    r"\bsuicid", r"\bkhudkushi\b", r"\bjaan\s+de", r"\bzeher\b", r"\bzahar\b", r"\bpoison", r"\boverdose\b",
    r"\bmarna\s+chah", r"\bkhud\s+ko\s+(?:khatam|nuksan)", r"\banaphyla", r"\bgala\s+band\b",
    rf"\b(?:bahut|bahot|bohot|tez|asahniya|unbearable|severe)\W+{W}{{0,2}}?(?:dard|pain)\b",
]
RED_RE = [re.compile(p, re.I) for p in RED_FLAGS]
NEGATION = {"nahi", "nahin", "nhi", "na", "no", "nope", "not", "never", "bilkul", "koi"}

ADVICE_RE = re.compile(
    r"(?=.*\b(?:goli|tablet|dawai|dawa|medicine|dose|dosage|syrup|crocin|paracetamol|calpol|ibuprofen|antibiotic)\b)"
    r"(?=.*(?:le\s+lun|le\s+loon|le\s+sakta|le\s+sakti|kitni|kitne|kab\s+tak|ya\s+nahi|should\s+i|can\s+i\s+take|kya\s+lu|kya\s+karun|kya\s+khau|kaise\s+le|how\s+(?:much|many)))",
    re.I | re.S)
ADVICE2_RE = re.compile(r"\b(?:kya\s+karun|kya\s+karna\s+chahiye|is\s+it\s+safe|side\s+effects?|kya\s+yeh\s+serious)\b", re.I)
OOS_RE = re.compile(r"\b(?:bill|billing|invoice|fees?|insurance|refund|lab\s+report|test\s+results?|prescription|certificate)\b", re.I)

INJECTION_RE = re.compile(
    r"(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:your\s+|the\s+|my\s+)?(?:previous|prior|above|earlier)?\s*(?:instructions|rules|prompt|guidelines)"
    r"|administrator\s+mode|admin\s+mode|developer\s+mode|system\s+prompt|you\s+are\s+now|jailbreak"
    r"|authori[sz]ed\s+internal\s+test|this\s+is\s+a\s+test|override|reveal\s+your|pretend\s+(?:to\s+be|you)|\bsudo\b|bypass|new\s+instructions", re.I)
BULK_RE = re.compile(
    r"(?:cancel|delete|remove|radd|hata)\w*.*\b(?:every|all|sabhi|saare|sab)\b.*appointments?"
    r"|\b(?:every|all|sabhi|saare|sab)\s+appointments?\b", re.I | re.S)


def red_flag(text: str) -> Optional[str]:
    """First red-flag phrase found that is not directly negated ('seene mein dard nahi hai')."""
    hits = []
    for rx in RED_RE:
        for m in rx.finditer(text):
            after = re.findall(r"[a-z']+", text[m.end():m.end() + 40].lower())[:2]
            before = re.findall(r"[a-z']+", text[max(0, m.start() - 25):m.start()].lower())[-1:]
            negated = any(t in NEGATION for t in after) or before == ["no"]
            if not negated:
                hits.append(m.group(0))
    return hits[0] if hits else None


def medical_advice(text: str) -> bool:
    return bool(ADVICE_RE.search(text) or ADVICE2_RE.search(text))


def strip_injection(text: str) -> tuple[list[str], bool]:
    """Drop injected or bulk-action sentences; keep the rest. Returns (kept_sentences, saw_injection)."""
    safe = re.sub(r"\b(Dr|Mr|Mrs|Ms)\.", r"\1", text)
    safe = re.sub(r"\b([A-Z])\.", r"\1", safe)
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n", safe) if p.strip()]
    kept, flagged = [], False
    for p in parts:
        if INJECTION_RE.search(p) or BULK_RE.search(p):
            flagged = True
        else:
            kept.append(p)
    return kept, flagged


# ======================================================================================
# 2. Dates, times, doctors (Hinglish)
# ======================================================================================
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"])}
for _s, _n in {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}.items():
    MONTHS[_s] = _n
WEEKDAYS = {}
for _i, _ws in enumerate([
    ["monday", "somwar", "somvar", "sombar"], ["tuesday", "mangalwar", "mangalvar"], ["wednesday", "budhwar", "budhvar"],
    ["thursday", "guruwar", "guruvar", "brihaspatiwar", "veerwar"], ["friday", "shukrawar", "shukravar"],
    ["saturday", "shaniwar", "shanivaar", "shanivar"], ["sunday", "raviwar", "ravivar", "itwar", "itwaar"]]):
    for _w in _ws:
        WEEKDAYS[_w] = _i
HINDI_NUM = {"ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6, "che": 6, "chah": 6,
             "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12, "baarah": 12}
NUM_WORDS = "|".join(sorted(HINDI_NUM, key=len, reverse=True))
PERIODS = {"morning": ("subah", "morning", "savere"), "afternoon": ("dopahar", "afternoon", "dophar"),
           "evening": ("shaam", "evening", "sham"), "night": ("raat", "night")}
WINDOWS = {"morning": ("09:00", "12:00"), "afternoon": ("12:00", "16:00"), "evening": ("16:00", "20:00"), "night": ("18:00", "23:59")}
ANY_TIME = re.compile(r"\bkoi\s+bhi\s+(?:time|samay)\b|\bany\s*time\b|\bkabhi\s+bhi\b", re.I)
CORRECTION = re.compile(r"\b(?:nah(?:i|in)\s+nah(?:i|in)|no\s+no|nah(?:i|in)|sorry|i\s+mean|matlab|actually|ruko)\b[\s,.…-]*", re.I)
DR = {"dr_rao": re.compile(r"\brao\b", re.I), "dr_sethi": re.compile(r"\bsethi\b", re.I)}
PHONE_RE = re.compile(r"(?<![\d-])(?:\+?91[\s-]?)?(\d[\d\s-]{8,13}\d)(?![\d])")


def find_phone(text: str) -> tuple[Optional[str], str]:
    """Returns (phone digits or None, text with phone numbers blanked out)."""
    phone = None

    def repl(m):
        nonlocal phone
        d = re.sub(r"\D", "", m.group(0))
        d = d[-10:] if len(d) >= 10 else d
        if len(d) == 10:
            phone = d
            return " "
        return m.group(0)
    cleaned = PHONE_RE.sub(repl, text)
    return phone, cleaned


def _clamp_day(y: int, m: int, d: int) -> Optional[dt.date]:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def day_of_month(today: dt.date, d: int, month: Optional[int] = None) -> Optional[dt.date]:
    if month:
        cand = _clamp_day(today.year, month, d)
        if cand and cand < today:
            cand = _clamp_day(today.year + 1, month, d)
        return cand
    y, m = today.year, today.month
    for _ in range(14):
        cand = _clamp_day(y, m, d)
        if cand and cand >= today:
            return cand
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


@dataclass
class DateMentions:
    dates: list = field(default_factory=list)       # [(pos, date)] relative and weekday mentions, in order
    explicit: list = field(default_factory=list)    # [(pos, date)] day-of-month / full dates
    weekdays: list = field(default_factory=list)    # [(pos, weekday_index)]
    dobs: list = field(default_factory=list)


def parse_dates(seg: str, today: dt.date) -> DateMentions:
    s = seg.lower()
    out = DateMentions()
    s = re.sub(r"\bkal\s+ke\s+baad\b", "parso", s)
    taken: list[tuple[int, int]] = []

    def free(m):
        return not any(a < m.end() and m.start() < b for a, b in taken)

    def take(m):
        taken.append((m.start(), m.end()))

    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", s):
        d = _clamp_day(int(m[1]), int(m[2]), int(m[3]))
        if d:
            take(m)
            (out.dobs if d < today else out.explicit).append((m.start(), d) if d >= today else d)
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b", s):
        if not free(m):
            continue
        d = _clamp_day(int(m[3]), int(m[2]), int(m[1]))
        if d:
            take(m)
            (out.dobs if d < today else out.explicit).append((m.start(), d) if d >= today else d)
    mon = "|".join(sorted(MONTHS, key=len, reverse=True))
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:of\s+)?({mon})\b", s):
        if free(m):
            d = day_of_month(today, int(m[1]), MONTHS[m[2]])
            if d:
                take(m); out.explicit.append((m.start(), d))
    for m in re.finditer(rf"\b({mon})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", s):
        if free(m):
            d = day_of_month(today, int(m[2]), MONTHS[m[1]])
            if d:
                take(m); out.explicit.append((m.start(), d))
    for m in re.finditer(rf"\b(\d{{1,2}}|{NUM_WORDS})\s*(?:tareekh|tarikh|taarikh|tarik)\b|\b(?:tareekh|tarikh|date)\s*(\d{{1,2}})\b|\b(\d{{1,2}})(?:st|nd|rd|th)\b", s):
        if not free(m):
            continue
        raw = m[1] or m[2] or m[3]
        n = int(raw) if raw.isdigit() else HINDI_NUM[raw]
        d = day_of_month(today, n)
        if d:
            take(m); out.explicit.append((m.start(), d))
    rel = [(r"\bparso\b|\bparson\b|\bday\s+after\s+tomorrow\b", 2), (r"\bkal\b|\btomorrow\b", 1), (r"\baaj\b|\btoday\b", 0)]
    for pat, delta in rel:
        for m in re.finditer(pat, s):
            if free(m):
                take(m); out.dates.append((m.start(), today + dt.timedelta(days=delta)))
    for m in re.finditer(r"\b(" + "|".join(WEEKDAYS) + r")\b", s):
        if free(m):
            wd = WEEKDAYS[m[1]]
            delta = (wd - today.weekday()) % 7 or 7  # the same weekday said on that weekday means next week
            take(m); out.weekdays.append((m.start(), wd)); out.dates.append((m.start(), today + dt.timedelta(days=delta)))
    out.dates.sort(); out.explicit.sort(key=lambda x: x[0] if isinstance(x, tuple) else 0)
    return out


@dataclass
class TimeMention:
    pos: int
    hour: int
    minute: int
    ampm: Optional[str]
    colon: bool


def parse_times(seg: str) -> list[TimeMention]:
    s = seg.lower()
    found: list[TimeMention] = []
    spans: list[tuple[int, int]] = []

    def add(m, h, mi, ap, colon):
        if not any(a < m.end() and m.start() < b for a, b in spans):
            spans.append((m.start(), m.end())); found.append(TimeMention(m.start(), h, mi, ap, colon))
    for m in re.finditer(r"\b(\d{1,2}):(\d{2})\s*(am|pm)?", s):
        if int(m[1]) <= 24 and int(m[2]) < 60:
            add(m, int(m[1]) % 24, int(m[2]), m[3], True)
    for m in re.finditer(rf"\b(?:(saade|sade|sawa|sava|paune|dedh|dhai)\s*)?(\d{{1,2}}|{NUM_WORDS})?\s*(?:baje|bje|am\b|pm\b|o'?clock)", s):
        pre, raw = m[1], m[2]
        if pre in ("dedh", "dhai") and not raw:
            h, mi = (1, 30) if pre == "dedh" else (2, 30)
        elif raw:
            h = int(raw) if raw.isdigit() else HINDI_NUM[raw]
            mi = {"saade": 30, "sade": 30, "sawa": 15, "sava": 15}.get(pre or "", 0)
            if pre == "paune":
                h, mi = (h - 1) or 12, 45
        else:
            continue
        ap = "am" if re.search(r"\bam\b", m[0]) else "pm" if re.search(r"\bpm\b", m[0]) else None
        if 1 <= h <= 24:
            add(m, h % 24, mi, ap, False)
    return sorted(found, key=lambda t: t.pos)


def period_of(seg: str) -> Optional[str]:
    s = seg.lower()
    for name, words in PERIODS.items():
        if any(re.search(rf"\b{w}\b", s) for w in words):
            return name
    return None


def resolve_time(t: TimeMention, period: Optional[str]) -> str:
    h = t.hour
    if t.ampm == "pm" and h < 12:
        h += 12
    elif t.ampm == "am" and h == 12:
        h = 0
    elif not t.ampm and not (t.colon and t.hour >= 13) and h < 12:
        if period in ("afternoon", "evening", "night"):
            h = h + 12 if h != 12 else 12
        elif period is None and 1 <= h <= 8:
            h += 12  # clinic hours run 09:00-20:00, so a bare "5 baje" is the evening
    return f"{h:02d}:{t.minute:02d}"


# ======================================================================================
# 3. Names, relations, intent
# ======================================================================================
STOP = set("""main mai mera mere meri mujhe hum hamara namaste namaskar hello hi haan han ji accha achha theek thik bas kal aaj parso parson subah shaam dopahar raat koi
waise ek arre are ignore cancel please this you the dr i my can do is are number abhi woh wo yeh ye bhi ab toh to aur ya lekin par rao sethi
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september october november december
somwar mangalwar budhwar guruwar shukrawar shaniwar shanivaar raviwar sorry ruko phir fir kya kaise nahi nahin haanji""".split()) | set(WEEKDAYS)
REL_CHILD = r"bete|beta|beti|bachcha|bachha|bacha|bachchi|bacche|baccha|son|daughter|child|kid|ladka|ladki"
REL_OTHER = r"pita|papa|father|dad|mummy|maa|mother|mom|biwi|wife|patni|pati|husband|bhai|behen|bahen|dost|friend|padosi|neighbou?r|dada|dadi|nana|nani|sasur|saas|chacha|mama|bhabhi|brother|sister|relative"
REL_RE = re.compile(rf"\b({REL_CHILD}|{REL_OTHER})\b", re.I)
CHILD_RE = re.compile(rf"\b({REL_CHILD})\b", re.I)
TOK_RE = re.compile(r"[A-Za-z][A-Za-z.'’]*")


@dataclass
class Names:
    caller: Optional[str] = None
    subject: Optional[str] = None


def vocab_from(patients: list[dict]) -> set[str]:
    return {k for p in patients for k in names.keys(p["name"])}


def extract_names(text: str, vocab: set[str]) -> Names:
    """Find capitalised name runs and decide whether each names the caller or the person the call is about."""
    out = Names()
    toks = [(m.group(0), m.start(), m.end()) for m in TOK_RE.finditer(text)]
    i = 0
    while i < len(toks):
        if not toks[i][0][0].isupper():
            i += 1
            continue
        j = i
        # a run continues only across plain whitespace: "Meera Joshi, 98122..., Kabir" is two names
        while j + 1 < len(toks) and toks[j + 1][0][0].isupper() and text[toks[j][2]:toks[j + 1][1]].strip(" \t") == "":
            j += 1
        run = [t for t in toks[i:j + 1]]
        i = j + 1
        words = [t for t in run if re.sub(r"[.'’]s?$", "", t[0]).lower() not in STOP and t[0].lower().rstrip(".") not in names.HONORIFICS]
        if not words:
            continue
        known = [names.key(re.sub(r"['’]s$", "", w[0])) in vocab for w in words]
        before = text[:words[0][1]]
        after = text[words[-1][2]:]
        # an unknown name is only believed when the sentence frames it as an introduction
        cued = bool((re.search(r"(?:\bmain|\bmai|\bnaam|name\s+is|this\s+is|i\s+am|i'm)\s*$", before, re.I)
                     and re.match(r"\s*(?:[,.!?]|$|hoon|hu|bol|speaking)", after, re.I))
                    or re.match(r"\W*(?:hoon|hu|bol|speaking|here|number|phone|mobile|\d)", after, re.I))
        if any(known):
            best, cur = [], []
            for w, k in zip(words, known):
                cur = cur + [w] if k else []
                best = cur if len(cur) > len(best) else best
            words = best
        elif not cued:
            continue
        name = " ".join(re.sub(r"['’]s$", "", w[0]).rstrip(".") if len(w[0]) > 2 else w[0] for w in words)
        a2 = text[words[-1][2]:]
        b2 = text[:words[0][1]]
        subject = bool(re.match(r"\W*(?:ji\b\W*)?(?:ka|ki|ke|ko)\b", a2, re.I)
                       or re.match(r"\W*(?:mera|meri|mere)\b", a2, re.I)
                       or re.search(rf"\b(?:{REL_CHILD}|{REL_OTHER})\b\s*(?:ka|ki|ko|ke)?\s*$", b2, re.I))
        if subject:
            out.subject = name
        else:
            out.caller = name
    return out


def parse_relation(text: str) -> Optional[str]:
    m = REL_RE.search(text)
    if not m:
        return None
    return "child" if CHILD_RE.fullmatch(m.group(1)) else m.group(1).lower()


CANCEL_RE = re.compile(r"\bcancel\b|\bradd\b|\brad\s+kar|hata\s*(?:do|dijiye|dena)|nahi\s+aa\s+pa|aa\s+nahi\s+sak", re.I)
RESCHEDULE_RE = re.compile(r"re-?schedule|\bbadal|\bshift\b|\bmove\b|postpone|aage\s+badha|\btaal|\b(?:use|isse|isko|usse|usko)\b.*\bkar(?:wa|na|dijiye|do)", re.I | re.S)
BOOK_RE = re.compile(r"appointment|\bbook|dikhana|dikhwana|milna|karwana|chahiye|\bslot\b|aa\s+sakta|aa\s+sakti|\bvisit\b", re.I)


def parse_intent(text: str) -> Optional[str]:
    if CANCEL_RE.search(text):
        return "cancel"
    if RESCHEDULE_RE.search(text):
        return "reschedule"
    if BOOK_RE.search(text):
        return "book"
    return None


# ======================================================================================
# 4. Facts: everything the caller said, merged across turns (the last mention wins)
# ======================================================================================
@dataclass
class Facts:
    intent: Optional[str] = None
    doctor: Optional[str] = None
    date: Optional[dt.date] = None
    date_alts: list = field(default_factory=list)
    source_date: Optional[dt.date] = None
    date_unclear: bool = False
    time: Optional[str] = None
    time_alts: list = field(default_factory=list)
    window: Optional[str] = None
    any_time: bool = False
    caller_name: Optional[str] = None
    subject_name: Optional[str] = None
    relation: Optional[str] = None
    phone: Optional[str] = None
    dob: Optional[str] = None

    @property
    def has_content(self) -> bool:
        return any([self.intent, self.doctor, self.date, self.date_unclear, self.time, self.window, self.any_time,
                    self.caller_name, self.subject_name, self.relation, self.phone, self.dob])


def extract(turns: list[str], today: dt.date, vocab: set[str]) -> Facts:
    f = Facts()
    for turn in turns:
        phone, text = find_phone(turn)
        if phone:
            f.phone = phone
        nm = extract_names(text, vocab)
        f.caller_name = nm.caller or f.caller_name
        f.subject_name = nm.subject or f.subject_name
        rel = parse_relation(text)
        f.relation = rel or f.relation
        f.intent = parse_intent(text) or f.intent
        if ANY_TIME.search(text):
            f.any_time = True
        carried: Optional[str] = None
        for seg in [s for s in CORRECTION.split(text) if s and s.strip()]:
            for code, rx in DR.items():
                if rx.search(seg):
                    f.doctor = code
            per = period_of(seg) or carried
            carried = period_of(seg) or carried
            dm = parse_dates(seg, today)
            f.dob = next((d.isoformat() for d in dm.dobs), f.dob)
            if dm.explicit:
                dates = [d for _, d in dm.explicit]
                wd = dm.weekdays[-1][1] if dm.weekdays else None
                if wd is not None and dates[-1].weekday() != wd:
                    f.date, f.date_alts, f.date_unclear = None, [], True  # "Tuesday the 7th" is not a real day: ask, don't guess
                else:
                    f.date, f.date_alts, f.date_unclear, f.source_date = dates[-1], [], False, None
            elif dm.dates:
                dates = [d for _, d in dm.dates]
                distinct = list(dict.fromkeys(dates))
                either = bool(re.search(r"\bya\b|\bor\b", seg, re.I)) and len(distinct) > 1
                f.date_unclear = False
                if either:
                    f.date, f.date_alts, f.source_date = distinct[0], distinct, None
                elif len(distinct) >= 2:
                    f.source_date, f.date, f.date_alts = distinct[0], distinct[-1], []
                else:
                    f.date, f.date_alts, f.source_date = distinct[-1], [], None
            tms = parse_times(seg)
            if tms:
                resolved = [resolve_time(t, per) for t in tms]
                either = bool(re.search(r"\bya\b|\bor\b", seg, re.I)) and len(set(resolved)) > 1
                f.time_alts = list(dict.fromkeys(resolved)) if either else []
                f.time = resolved[0] if either else resolved[-1]
                f.window = None
                f.any_time = False
            elif period_of(seg):
                f.window, f.time, f.time_alts = period_of(seg), None, []
    if f.intent is None and f.doctor:
        f.intent = "book"
    return f
