"""Conversation policy. The order is the safety argument:

  1. Every turn passes the safety gate (red flags, medical advice) on its RAW text before it is parsed.
  2. Injected / bulk-action sentences are dropped; the genuine request in the same turn is still served.
  3. Only after the LAST turn has cleared the gate do any tool calls happen. Nothing is written earlier,
     so an emergency that arrives after every booking detail is complete still cannot produce a booking.
  4. Identity and authority are decided from tool results (lookup_patient), never from the model's say-so.
  5. Replies are built from templates over tool results and checked by grounding.ungrounded().
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass, field
from typing import Optional

from . import names, nlu
from .grounding import human_date, ungrounded
from .store import ClinicStore
from .tools import safe_execute

CLOSED_TEXT = {
    "holiday": "the clinic is closed that day for a holiday",
    "doctor_on_leave": "the doctor is on leave that day",
    "no_working_hours": "the doctor does not see patients on that day of the week",
    "fully_booked": "every slot that day is already booked",
}
FALLBACK = "I'm sorry, I couldn't put together a reliable answer, so I haven't changed anything. Our staff can help."


class Finish(Exception):
    def __init__(self, state, reason=None, reply="", patient_id=None, appointment_id=None):
        self.state, self.reason, self.reply = state, reason, reply
        self.patient_id, self.appointment_id = patient_id, appointment_id


@dataclass
class Ident:
    patient_id: str
    name: str
    verified: bool
    appts: list = field(default_factory=list)
    on_behalf: bool = False


@dataclass
class RunOutput:
    result: dict
    transcript: list
    intent: Optional[str]
    caller_said: str


def summarise(name: str, res: dict) -> str:
    if not res["ok"]:
        return f"ERROR {res['error']['code']}: {res['error']['message']}"
    d = res["data"]
    if name == "lookup_patient":
        if d["mismatch"]:
            return "identifiers point to different patients"
        if d["match"]:
            return f"1 match: {d['match']['name']} ({d['match']['patient_id']}), {'verified' if d['verified'] else 'not verified'}"
        return f"{len(d['candidates'])} candidates" if d["candidates"] else "no match"
    if name == "search_slots":
        return f"closed: {d['closed_reason']}" if d["closed_reason"] else f"{len(d['available'])} free slots"
    if name in ("book_appointment", "reschedule_appointment", "cancel_appointment"):
        return f"{d['id']} {d['status']} {d['date']} {d['start']} ({d['doctor_id']})"
    if name == "escalate_to_human":
        return f"handoff {d['handoff_id']} {d['status']} ({d['reason']})"
    return "ok"


class Agent:
    def __init__(self, clinic: dict, cid: str, today: str, turns: list[str]):
        self.clinic, self.cid, self.today_s, self.turns = clinic, cid, today, turns
        self.today = dt.date.fromisoformat(today)
        self.store = ClinicStore(clinic, ":memory:", today)  # fresh clinic per run: nothing leaks between conversations
        self.vocab = nlu.vocab_from(clinic["patients"])
        self.calls: list[dict] = []
        self.results: list[dict] = []
        self.transcript: list[dict] = []
        self.injected = False
        self.facts = nlu.Facts()
        self.trigger_turn: Optional[str] = None

    # ---- tools ------------------------------------------------------------------------
    def tool(self, name: str, args: dict) -> dict:
        self.calls.append({"name": name, "arguments": args})
        res = safe_execute(self.store, name, args, self.cid)
        self.results.append(res)
        self.transcript.append({"kind": "tool", "name": name, "args": args, "ok": res["ok"], "summary": summarise(name, res)})
        return res

    def doc(self, doctor_id: str) -> str:
        return self.store.doctor_name(doctor_id)

    # ---- escalation replies -------------------------------------------------------------
    ESC = {
        "clinical_urgent": "What you describe sounds urgent. Please contact emergency services or get to the nearest hospital now rather than waiting for a clinic appointment. I've flagged this to our clinic staff as urgent, and I have not booked or changed any appointment.",
        "medical_advice": "I can't advise on medicines or symptoms; that needs a doctor or nurse. I've passed your question to our clinic staff. I have not booked or changed any appointment.",
        "not_authorised": "I can't change someone else's appointment unless you are their listed guardian. I've passed this to our clinic staff, and nothing has been changed.",
        "ambiguous_patient": "I can't tell for certain which patient record you mean, so I haven't booked or changed anything. I've asked our clinic staff to confirm with you.",
        "out_of_scope": "That's not something I can do at the front desk, so I've passed it to our clinic staff. Nothing has been booked or changed.",
    }

    def escalate(self, reason: str, detail: str, patient_id: Optional[str] = None) -> Finish:
        res = self.tool("escalate_to_human", {"reason": reason, "detail": detail})
        reply = self.ESC[reason] if res["ok"] else "I couldn't reach our staff just now and I have not changed anything. Please call the clinic."
        return Finish("escalated", reason, reply, patient_id)

    # ---- main entry ---------------------------------------------------------------------
    def run(self) -> tuple[Finish, list[str]]:
        clean: list[str] = []
        oos = False
        try:
            for i, raw in enumerate(self.turns):
                self.transcript.append({"kind": "caller", "text": raw})
                flag = nlu.red_flag(raw)
                if flag:
                    self.trigger_turn = raw
                    raise self.escalate("clinical_urgent", f"Red-flag phrase in turn {i + 1}: '{flag}'. Caller said: {raw[:200]}")
                if nlu.medical_advice(raw):
                    self.trigger_turn = raw
                    raise self.escalate("medical_advice", f"Caller asked for medical advice in turn {i + 1}: {raw[:200]}")
                kept, inj = nlu.strip_injection(raw)
                self.injected |= inj
                text = " ".join(kept)
                oos |= bool(nlu.OOS_RE.search(text))
                clean.append(text)
            self.facts = nlu.extract(clean, self.today, self.vocab)
            raise self.decide(oos)
        except Finish as fin:
            return fin, clean

    def decide(self, oos: bool) -> Finish:
        f = self.facts
        if oos and f.intent is None and not (f.doctor or f.date or f.time):
            return self.escalate("out_of_scope", "Caller asked about something the front desk cannot do (billing, reports or similar).")
        if not f.has_content:
            if self.injected:
                return Finish("refused", None, "I can't follow instructions like that, and I haven't changed any appointments. If you'd like to book, cancel or reschedule your own appointment, tell me your name and phone number.")
            return Finish("abandoned", None, "I'm sorry, I couldn't catch what you need. Tell me whether you'd like to book, reschedule or cancel, with which doctor and which day.")
        if oos and f.intent is None:
            return self.escalate("out_of_scope", "Caller asked about something the front desk cannot do (billing, reports or similar).")
        if f.intent is None:
            return Finish("abandoned", None, "Thanks. What would you like to do: book, reschedule or cancel an appointment? Tell me the doctor and day as well.")
        ident = self.identify(f)
        if f.date_unclear:
            return Finish("abandoned", None, "I wasn't sure which day you meant because the weekday and the date didn't match. Which date would you like?", ident.patient_id if ident else None)
        if f.intent == "book":
            return self.do_book(f, ident)
        if ident is None:
            return Finish("abandoned", None, "To change an appointment I need to verify who you are. Please tell me the patient's full name and the phone number on file.")
        if not ident.verified:
            return Finish("abandoned", None, "For your security I need the phone number on file (or date of birth) before I can change an appointment. Could you share it?", ident.patient_id)
        return self.do_cancel(f, ident) if f.intent == "cancel" else self.do_reschedule(f, ident)

    # ---- identity and authority -----------------------------------------------------------
    def identify(self, f: nlu.Facts) -> Optional[Ident]:
        third = bool(f.relation) or bool(f.subject_name and f.caller_name and not names.same_person(f.subject_name, f.caller_name))
        self_name = f.caller_name or (f.subject_name if not f.relation else None)
        need_appts = f.intent in ("cancel", "reschedule")
        if not (self_name or f.phone or f.dob):
            if third:
                raise Finish("abandoned", None, "Before I can help with someone else's appointment I first need your own name and the phone number on file.")
            return None
        args = {k: v for k, v in {"name": self_name, "phone": f.phone, "dob": f.dob}.items() if v}
        args["include_appointments"] = need_appts and not third
        res = self.tool("lookup_patient", args)
        if not res["ok"]:
            raise Finish("abandoned", None, f"I couldn't look that up: {res['error']['message']}")
        d = res["data"]
        if d["mismatch"]:
            raise self.escalate("ambiguous_patient", "The name and phone number given belong to different patient records.")
        if len(d["candidates"]) > 1:
            raise self.escalate("ambiguous_patient", f"{len(d['candidates'])} patient records match what the caller gave; no phone or date of birth to tell them apart.")
        if not d["candidates"]:
            if self_name and f.phone and not d["known_phone"]:
                raise self.escalate("out_of_scope", "No patient record matches the name and phone given. The front desk cannot register new patients.")
            if f.phone and d["known_phone"]:
                raise self.escalate("ambiguous_patient", "The phone number is on file but the name given does not match that record.")
            raise Finish("abandoned", None, "I couldn't find a record for that. Could you give me the full name and the phone number registered with the clinic?")
        caller = d["match"]
        if not third:
            return Ident(caller["patient_id"], caller["name"], d["verified"], d["appointments"])
        # Someone else's appointment. Authority comes from guardian_of and nothing else:
        # not a shared phone, not a relationship the caller states, not knowing the name.
        if not d["verified"]:
            raise Finish("abandoned", None, "To act for someone else I need to confirm who you are first. Please share the phone number or date of birth on file for you.", caller["patient_id"])
        wards = d["wards"]
        if f.subject_name:
            ids = names.best(f.subject_name, [(w["patient_id"], w["name"]) for w in wards])
        elif f.relation == "child":
            ids = [w["patient_id"] for w in wards]
        else:
            ids = []
        if not ids:
            raise self.escalate("not_authorised", f"Caller ({caller['patient_id']}) asked to act for someone who is not a listed ward.", caller["patient_id"])
        if len(ids) > 1:
            raise self.escalate("ambiguous_patient", f"Caller is guardian of {len(ids)} children and did not say which one.", caller["patient_id"])
        ward = next(w for w in wards if w["patient_id"] == ids[0])
        r2 = self.tool("lookup_patient", {"patient_id": ward["patient_id"], "include_appointments": need_appts})
        appts = r2["data"]["appointments"] if r2["ok"] else []
        return Ident(ward["patient_id"], ward["name"], True, appts, on_behalf=True)

    # ---- choosing a slot ---------------------------------------------------------------------
    def choose(self, f: nlu.Facts, avail: list[str]) -> tuple[Optional[str], str]:
        cands = f.time_alts or ([f.time] if f.time else [])
        if cands:
            for t in cands:
                if t in avail:
                    return t, ""
            return None, "time_taken"
        if f.window or f.any_time:
            lo, hi = nlu.WINDOWS[f.window] if f.window else ("00:00", "23:59")
            pick = next((s for s in avail if lo <= s < hi), None)
            return (pick, "") if pick else (None, "window_full")
        return None, "no_time"

    @staticmethod
    def times(ts: list[str], n: int = 8) -> str:
        return ", ".join(ts[:n]) + (" and more" if len(ts) > n else "")

    def plan_slot(self, f: nlu.Facts, doctor_id: str, dates: list[dt.date], patient_id: Optional[str]) -> tuple[str, str, dict]:
        first: Optional[str] = None
        doc = self.doc(doctor_id)
        for d in dates:
            iso = d.isoformat()
            res = self.tool("search_slots", {"doctor_id": doctor_id, "date": iso})
            if not res["ok"]:
                msg = f"I couldn't check that day: {res['error']['message']}"
            else:
                data = res["data"]
                if data["closed_reason"]:
                    msg = f"{doc} isn't available on {human_date(iso)}: {CLOSED_TEXT[data['closed_reason']]}. Would another day work?"
                else:
                    start, why = self.choose(f, data["available"])
                    if start:
                        return iso, start, data
                    free = self.times(data["available"])
                    msg = {
                        "time_taken": f"That time isn't free with {doc} on {human_date(iso)}. Free times: {free}. Which would you like?",
                        "window_full": f"{doc} has no free {f.window or 'matching'} slots on {human_date(iso)}. Free times that day: {free}. Which would you like?",
                        "no_time": f"{doc} has openings on {human_date(iso)}: {free}. What time suits you?",
                    }[why]
            first = first or msg
        raise Finish("abandoned", None, first or "I couldn't find a suitable slot.", patient_id)

    # ---- the three actions ---------------------------------------------------------------------
    def do_book(self, f: nlu.Facts, ident: Optional[Ident]) -> Finish:
        pid = ident.patient_id if ident else None
        if not f.doctor:
            return Finish("abandoned", None, "Which doctor would you like to see: Dr. Rao or Dr. Sethi?", pid)
        if not f.date:
            return Finish("abandoned", None, f"Which day would you like to see {self.doc(f.doctor)}?", pid)
        iso, start, _ = self.plan_slot(f, f.doctor, f.date_alts or [f.date], pid)
        if ident is None:
            return Finish("abandoned", None, f"{self.doc(f.doctor)} is free at {start} on {human_date(iso)}. To book it I need the patient's full name and phone number.")
        res = self.tool("book_appointment", {"patient_id": ident.patient_id, "doctor_id": f.doctor, "date": iso, "start": start})
        if not res["ok"]:
            return Finish("abandoned", None, f"I couldn't book that: {res['error']['message']} Nothing has been booked. Would you like to pick another time?", pid)
        a = res["data"]
        who = f" for {ident.name}" if ident.on_behalf else ""
        note = f" That was the earliest free time in the {f.window}." if f.window and not f.time else ""
        return Finish("booked", None, f"Booked{who}: {self.doc(f.doctor)} on {human_date(a['date'])} at {a['start']}. Your reference is {a['id']}.{note}", ident.patient_id, a["id"])

    def pick_appointment(self, f: nlu.Facts, ident: Ident, reschedule: bool) -> dict:
        appts = ident.appts
        if not appts:
            raise Finish("abandoned", None, f"I don't see any upcoming appointment for {ident.name}, so I haven't changed anything.", ident.patient_id)
        c = appts
        day = f.source_date if reschedule else f.date
        if day:
            c = [a for a in c if a["date"] == day.isoformat()]
        if f.doctor:
            by_doc = [a for a in c if a["doctor_id"] == f.doctor]
            c = by_doc or ([] if not reschedule else c)
        if len(c) == 1:
            return c[0]
        listing = "; ".join(f"{self.doc(a['doctor_id'])} on {human_date(a['date'])} at {a['start']}" for a in (c or appts)[:5])
        if not c:
            raise Finish("abandoned", None, f"I couldn't find an appointment matching that for {ident.name}. Upcoming ones: {listing}. Which one did you mean?", ident.patient_id)
        raise Finish("abandoned", None, f"{ident.name} has more than one matching appointment: {listing}. Which one should I change?", ident.patient_id)

    def do_cancel(self, f: nlu.Facts, ident: Ident) -> Finish:
        a = self.pick_appointment(f, ident, reschedule=False)
        res = self.tool("cancel_appointment", {"appointment_id": a["id"], "patient_id": ident.patient_id})
        if not res["ok"]:
            return Finish("abandoned", None, f"I couldn't cancel that: {res['error']['message']} Nothing was changed.", ident.patient_id)
        r = res["data"]
        return Finish("cancelled", None, f"Cancelled: {self.doc(r['doctor_id'])} on {human_date(r['date'])} at {r['start']} (reference {r['id']}).", ident.patient_id, r["id"])

    def do_reschedule(self, f: nlu.Facts, ident: Ident) -> Finish:
        a = self.pick_appointment(f, ident, reschedule=True)
        if not f.date:
            return Finish("abandoned", None, "Which day would you like to move it to?", ident.patient_id)
        if f.doctor and f.doctor != a["doctor_id"]:
            # Moving to a different doctor is a new booking decision, not a reschedule. Ask rather than guess.
            return Finish("abandoned", None, f"That appointment is with {self.doc(a['doctor_id'])}. I can move it to another time with the same doctor; to see {self.doc(f.doctor)} instead we'd need to cancel and book again. What would you like?", ident.patient_id)
        doctor_id = a["doctor_id"]
        iso, start, _ = self.plan_slot(f, doctor_id, f.date_alts or [f.date], ident.patient_id)
        res = self.tool("reschedule_appointment", {"appointment_id": a["id"], "patient_id": ident.patient_id, "doctor_id": doctor_id, "date": iso, "start": start})
        if not res["ok"]:
            return Finish("abandoned", None, f"I couldn't move it: {res['error']['message']} Your original appointment is unchanged.", ident.patient_id)
        r = res["data"]
        p = r["previous"]
        return Finish("rescheduled", None, f"Moved: {self.doc(r['doctor_id'])} now on {human_date(r['date'])} at {r['start']} (was {human_date(p['date'])} at {p['start']}). Reference {r['id']}.", ident.patient_id, r["id"])


def run_conversation(clinic: dict, conversation_id: str, today: str, turns: list[str]) -> RunOutput:
    t0 = time.perf_counter()
    ag = Agent(clinic, conversation_id, today, turns)
    fin, _ = ag.run()
    reply = fin.reply
    if ag.injected and fin.state not in ("refused", "escalated"):
        reply += " (I ignored part of your message that asked me to do something I can't do.)"
    sources = [ag.calls, ag.results, ag.turns, today]
    bad = ungrounded(reply, sources)
    if bad:
        ag.transcript.append({"kind": "guard", "text": f"Reply withheld: ungrounded facts {bad}"})
        reply = FALLBACK
    ag.transcript.append({"kind": "agent", "text": reply})
    result = {
        "conversation_id": conversation_id,
        "tool_calls": ag.calls,
        "terminal_state": fin.state,
        "escalation_reason": fin.reason,
        "patient_id": fin.patient_id,
        "appointment_id": fin.appointment_id,
        "reply": reply,
        "metrics": {"turns": len(turns), "tokens": 0, "latency_ms": int((time.perf_counter() - t0) * 1000)},
    }
    caller_said = ag.trigger_turn or next((t for t in turns if t.strip()), "")
    return RunOutput(result, ag.transcript, ag.facts.intent, caller_said[:160])
