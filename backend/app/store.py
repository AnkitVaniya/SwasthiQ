"""The tool layer: one SQLite database, six tools, every write in one transaction.
Nothing in this file knows an LLM exists. The agent can ask for anything; this layer decides."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
from contextlib import contextmanager

from . import names

REASONS = ("clinical_urgent", "medical_advice", "not_authorised", "ambiguous_patient", "out_of_scope")
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

SCHEMA = """
CREATE TABLE patients (id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL, dob TEXT, guardian_of TEXT NOT NULL);
CREATE TABLE doctors (id TEXT PRIMARY KEY, name TEXT NOT NULL, speciality TEXT, windows TEXT NOT NULL, leave_dates TEXT NOT NULL);
CREATE TABLE holidays (date TEXT PRIMARY KEY);
CREATE TABLE appointments (
  seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
  patient_id TEXT NOT NULL REFERENCES patients(id), doctor_id TEXT NOT NULL REFERENCES doctors(id),
  date TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('booked','cancelled')));
-- The storage-level guarantee: two live bookings can never share a doctor, date and start.
CREATE UNIQUE INDEX one_live_booking_per_slot ON appointments(doctor_id, date, start) WHERE status = 'booked';
CREATE INDEX appt_by_patient ON appointments(patient_id, status);
CREATE TABLE handoffs (id TEXT PRIMARY KEY, conversation_id TEXT, reason TEXT NOT NULL, detail TEXT, status TEXT NOT NULL);
"""


class ToolError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.field, self.status = code, message, field, status

    def to_dict(self) -> dict:
        err = {"code": self.code, "message": self.message}
        if self.field:
            err["field"] = self.field
        return {"ok": False, "error": err}


def to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def to_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class ClinicStore:
    def __init__(self, clinic: dict, path: str = ":memory:", today: str | None = None):
        self.clinic = clinic
        self.path = path
        self.slot = int(clinic["clinic"].get("slot_minutes", 15))
        self.today = today or clinic["clinic"]["reference_date"]
        self._lock = threading.RLock()  # one connection shared by threads; the lock serialises use of it
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self.reset()

    # ---- infrastructure ---------------------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            c = self._conn
            for t in ("handoffs", "appointments", "holidays", "doctors", "patients"):
                c.execute(f"DROP TABLE IF EXISTS {t}")
            c.executescript(SCHEMA)
            c.execute("BEGIN IMMEDIATE")
            d = self.clinic
            c.executemany("INSERT INTO patients VALUES (?,?,?,?,?)",
                          [(p["id"], p["name"], p["phone"], p.get("dob"), json.dumps(p.get("guardian_of", []))) for p in d["patients"]])
            c.executemany("INSERT INTO doctors VALUES (?,?,?,?,?)",
                          [(x["id"], x["name"], x.get("speciality"), json.dumps(x["windows"]), json.dumps(x.get("leave_dates", []))) for x in d["doctors"]])
            c.executemany("INSERT INTO holidays VALUES (?)", [(h,) for h in d.get("holidays", [])])
            for a in d["appointments"]:
                c.execute("INSERT INTO appointments(id,patient_id,doctor_id,date,start,end,status) VALUES (?,?,?,?,?,?,?)",
                          (a["id"], a["patient_id"], a["doctor_id"], a["date"], a["start"], a["end"], a["status"]))
            c.execute("COMMIT")

    @contextmanager
    def _tx(self):
        """BEGIN IMMEDIATE takes the write lock up front, so validate-then-write has no gap."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _q(self, sql: str, *a):
        with self._lock:
            return self._conn.execute(sql, a).fetchall()

    # ---- helpers ------------------------------------------------------------------------
    def _doctor(self, c, doctor_id: str):
        row = c.execute("SELECT * FROM doctors WHERE id=?", (doctor_id,)).fetchone()
        if not row:
            raise ToolError("unknown_doctor", f"No doctor with id '{doctor_id}'.", "doctor_id", 404)
        return row

    def _patient(self, c, patient_id: str):
        row = c.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
        if not row:
            raise ToolError("unknown_patient", f"No patient with id '{patient_id}'.", "patient_id", 404)
        return row

    def _check_date(self, date: str) -> None:
        if date < self.today:
            raise ToolError("date_in_past", f"{date} is before today ({self.today}).", "date", 422)

    def _day(self, c, doc, date: str) -> tuple[list[str], str | None]:
        """All bookable start times that day, and why the day is closed (or None)."""
        if c.execute("SELECT 1 FROM holidays WHERE date=?", (date,)).fetchone():
            return [], "holiday"
        if date in json.loads(doc["leave_dates"]):
            return [], "doctor_on_leave"
        wd = DAYS[dt.date.fromisoformat(date).weekday()]
        starts: set[int] = set()
        for w in json.loads(doc["windows"]):
            if w["day"] == wd:  # overlapping windows collapse: one start time is one slot
                starts.update(range(to_min(w["start"]), to_min(w["end"]) - self.slot + 1, self.slot))
        if not starts:
            return [], "no_working_hours"
        return [to_hm(t) for t in sorted(starts)], None

    def _taken(self, c, doctor_id: str, date: str) -> set[str]:
        return {r["start"] for r in c.execute(
            "SELECT start FROM appointments WHERE doctor_id=? AND date=? AND status='booked'", (doctor_id, date))}

    def _validate_target(self, c, patient_id: str, doctor_id: str, date: str, start: str, ignore_id: str | None = None) -> None:
        doc = self._doctor(c, doctor_id)
        self._check_date(date)
        slots, closed = self._day(c, doc, date)
        if closed:
            raise ToolError(closed, f"{doc['name']} is not available on {date} ({closed.replace('_', ' ')}).", "date")
        if start not in slots:
            raise ToolError("invalid_slot", f"{start} is not a bookable start time for {doc['name']} on {date}.", "start")
        if start in self._taken(c, doctor_id, date):
            raise ToolError("slot_taken", f"{doc['name']} already has a booking at {start} on {date}.", "start")
        clash = c.execute("SELECT id FROM appointments WHERE patient_id=? AND date=? AND start=? AND status='booked' AND id IS NOT ?",
                          (patient_id, date, start, ignore_id)).fetchone()
        if clash:
            raise ToolError("patient_conflict", f"The patient already has appointment {clash['id']} at {start} on {date}.", "start")

    def _appt(self, row) -> dict:
        return {k: row[k] for k in ("id", "patient_id", "doctor_id", "date", "start", "end", "status")}

    # ---- the six tools --------------------------------------------------------------------
    def search_slots(self, doctor_id: str, date: str) -> dict:
        with self._lock:
            c = self._conn
            doc = self._doctor(c, doctor_id)
            self._check_date(date)
            slots, closed = self._day(c, doc, date)
            taken = self._taken(c, doctor_id, date)
            free = [s for s in slots if s not in taken]
            if not closed and slots and not free:
                closed = "fully_booked"
            return {"doctor_id": doctor_id, "date": date, "available": free, "closed_reason": closed}

    def book_appointment(self, patient_id: str, doctor_id: str, date: str, start: str) -> dict:
        with self._tx() as c:
            self._patient(c, patient_id)
            self._validate_target(c, patient_id, doctor_id, date, start)
            n = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM appointments").fetchone()[0]
            end = to_hm(to_min(start) + self.slot)
            try:
                c.execute("INSERT INTO appointments(id,patient_id,doctor_id,date,start,end,status) VALUES (?,?,?,?,?,?, 'booked')",
                          (f"ap_{n:04d}", patient_id, doctor_id, date, start, end))
            except sqlite3.IntegrityError:  # the unique index caught what the checks above could not
                raise ToolError("slot_taken", f"That slot ({start} on {date}) was just taken.", "start")
            return self._appt(c.execute("SELECT * FROM appointments WHERE id=?", (f"ap_{n:04d}",)).fetchone())

    def reschedule_appointment(self, appointment_id: str, patient_id: str, doctor_id: str, date: str, start: str) -> dict:
        with self._tx() as c:
            row = c.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
            if not row:
                raise ToolError("unknown_appointment", f"No appointment '{appointment_id}'.", "appointment_id", 404)
            if row["patient_id"] != patient_id:
                raise ToolError("not_owner", "That appointment does not belong to this patient.", "appointment_id", 403)
            if row["status"] != "booked":
                raise ToolError("appointment_not_active", f"Appointment {appointment_id} is {row['status']}.", "appointment_id")
            if (row["doctor_id"], row["date"], row["start"]) == (doctor_id, date, start):
                raise ToolError("same_slot", "The appointment is already at that time.", "start")
            self._validate_target(c, patient_id, doctor_id, date, start, ignore_id=appointment_id)
            previous = self._appt(row)
            try:
                # one UPDATE moves the row: the old slot is freed by the statement that takes the new one
                c.execute("UPDATE appointments SET doctor_id=?, date=?, start=?, end=? WHERE id=? AND status='booked'",
                          (doctor_id, date, start, to_hm(to_min(start) + self.slot), appointment_id))
            except sqlite3.IntegrityError:
                raise ToolError("slot_taken", f"That slot ({start} on {date}) was just taken.", "start")
            new = self._appt(c.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())
            return {**new, "previous": previous}

    def cancel_appointment(self, appointment_id: str, patient_id: str) -> dict:
        with self._tx() as c:
            row = c.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone()
            if not row:
                raise ToolError("unknown_appointment", f"No appointment '{appointment_id}'.", "appointment_id", 404)
            if row["patient_id"] != patient_id:
                raise ToolError("not_owner", "That appointment does not belong to this patient.", "appointment_id", 403)
            if row["status"] == "cancelled":
                raise ToolError("already_cancelled", f"Appointment {appointment_id} is already cancelled.", "appointment_id")
            c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (appointment_id,))  # never deleted: ids stay unique
            return self._appt(c.execute("SELECT * FROM appointments WHERE id=?", (appointment_id,)).fetchone())

    def lookup_patient(self, name=None, phone=None, dob=None, patient_id=None, include_appointments=False) -> dict:
        if not any([name, phone, dob, patient_id]):
            raise ToolError("missing_identifier", "Give at least one of name, phone, dob or patient_id.", None, 422)
        with self._lock:
            c = self._conn
            allp = [dict(r) for r in c.execute("SELECT * FROM patients ORDER BY id")]
            pool = [p for p in allp if not patient_id or p["id"] == patient_id]
            if phone:
                pool = [p for p in pool if names.norm_phone(p["phone"]) == names.norm_phone(phone)]
            if dob:
                pool = [p for p in pool if p["dob"] == dob]
            if name:
                ids = set(names.best(name, [(p["id"], p["name"]) for p in pool]))
                pool = [p for p in pool if p["id"] in ids]
            # Did two identifiers each match somebody, just not the same somebody?
            by_name = bool(name) and bool(names.best(name, [(p["id"], p["name"]) for p in allp]))
            by_phone = bool(phone) and any(names.norm_phone(p["phone"]) == names.norm_phone(phone) for p in allp)
            by_dob = bool(dob) and any(p["dob"] == dob for p in allp)
            given = [bool(name), bool(phone), bool(dob)]
            mismatch = not pool and sum(given) >= 2 and sum([by_name, by_phone, by_dob]) >= 2
            one = pool[0] if len(pool) == 1 else None
            out = {
                "candidates": [{"patient_id": p["id"], "name": p["name"]} for p in pool],
                "match": {"patient_id": one["id"], "name": one["name"]} if one else None,
                "resolved": one is not None,
                # phone/dob that matched the single record is evidence of identity; a bare name is not
                "verified": bool(one and (phone or dob)),
                "mismatch": mismatch,
                "known_phone": by_phone,
                "wards": [], "appointments": [],
            }
            if one:
                wards = json.loads(one["guardian_of"])
                for w in wards:
                    r = c.execute("SELECT id,name FROM patients WHERE id=?", (w,)).fetchone()
                    if r:
                        out["wards"].append({"patient_id": r["id"], "name": r["name"]})
                if include_appointments:
                    rows = c.execute("SELECT * FROM appointments WHERE patient_id=? AND status='booked' AND date>=? ORDER BY date,start",
                                     (one["id"], self.today)).fetchall()
                    out["appointments"] = [self._appt(r) for r in rows]
            return out

    def escalate_to_human(self, reason: str, detail: str = "", conversation_id: str = "adhoc") -> dict:
        if reason not in REASONS:
            raise ToolError("invalid_reason", f"reason must be one of {list(REASONS)}.", "reason", 422)
        with self._tx() as c:
            n = c.execute("SELECT COUNT(*)+1 FROM handoffs").fetchone()[0]
            hid = f"ho_{n:04d}"
            c.execute("INSERT INTO handoffs VALUES (?,?,?,?, 'open')", (hid, conversation_id, reason, detail[:500]))
            return {"handoff_id": hid, "status": "open", "reason": reason}

    # ---- read-only views for tests and the live-state endpoint -----------------------------
    def appointments(self) -> list[dict]:
        return [self._appt(r) for r in self._q("SELECT * FROM appointments ORDER BY seq")]

    def doctor_name(self, doctor_id: str) -> str:
        for d in self.clinic["doctors"]:
            if d["id"] == doctor_id:
                return d["name"]
        return doctor_id
