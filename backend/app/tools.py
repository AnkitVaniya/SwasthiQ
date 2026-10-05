"""Argument validation and dispatch. Every tool call, from the agent or from HTTP, goes through here,
so a malformed call from a model gets a specific, fixable error rather than a crash."""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .store import ClinicStore, ToolError

HINTS = {
    "date": "a real calendar date formatted YYYY-MM-DD, e.g. 2026-10-03",
    "start": "a 24-hour time formatted HH:MM, e.g. 09:30",
    "doctor_id": "a doctor id such as 'dr_rao'",
    "patient_id": "a patient id returned by lookup_patient, such as 'pt_0014'",
    "appointment_id": "an appointment id such as 'ap_0007'",
    "reason": "one of clinical_urgent, medical_advice, not_authorised, ambiguous_patient, out_of_scope",
}


def _date(v: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        raise ValueError("is not formatted YYYY-MM-DD")
    try:
        dt.date.fromisoformat(v)
    except ValueError:
        raise ValueError("is not a real calendar date")
    return v


def _time(v: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", v)
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ValueError("is not a valid 24-hour HH:MM time")
    return f"{int(m[1]):02d}:{m[2]}"


class _B(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchSlots(_B):
    doctor_id: str
    date: str
    _d = field_validator("date")(_date)


class Book(_B):
    patient_id: str
    doctor_id: str
    date: str
    start: str
    _d = field_validator("date")(_date)
    _t = field_validator("start")(_time)


class Reschedule(Book):
    appointment_id: str


class Cancel(_B):
    appointment_id: str
    patient_id: str


class Lookup(_B):
    name: Optional[str] = None
    phone: Optional[str] = None
    dob: Optional[str] = None
    patient_id: Optional[str] = None
    include_appointments: bool = False

    @field_validator("dob")
    @classmethod
    def _dob(cls, v):
        return v if v is None else _date(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        if v is not None and len(re.sub(r"\D", "", v)) < 10:
            raise ValueError("must contain at least 10 digits")
        return v


class Escalate(_B):
    reason: str
    detail: str = ""


TOOLS: dict[str, type[BaseModel]] = {
    "search_slots": SearchSlots, "book_appointment": Book, "reschedule_appointment": Reschedule,
    "cancel_appointment": Cancel, "lookup_patient": Lookup, "escalate_to_human": Escalate,
}


def validate(name: str, args: Any) -> BaseModel:
    if name not in TOOLS:
        raise ToolError("unknown_tool", f"Unknown tool '{name}'. Valid tools: {sorted(TOOLS)}.", "name", 404)
    if not isinstance(args, dict):
        raise ToolError("invalid_arguments", "Tool arguments must be a JSON object.", "arguments", 422)
    model = TOOLS[name]
    try:
        return model(**args)
    except ValidationError as e:
        err = e.errors()[0]
        field = str(err["loc"][0]) if err["loc"] else "arguments"
        hint = HINTS.get(field)
        if err["type"] == "missing":
            raise ToolError("missing_argument", f"Missing required argument '{field}'" + (f": expected {hint}." if hint else "."), field, 422)
        if err["type"] == "extra_forbidden":
            raise ToolError("unknown_argument", f"Unexpected argument '{field}' for {name}. Allowed: {sorted(model.model_fields)}.", field, 422)
        msg = str(err["msg"]).replace("Value error, ", "")
        raise ToolError("invalid_argument", f"Argument '{field}' {msg}" + (f"; expected {hint}." if hint else "."), field, 422)


def execute(store: ClinicStore, name: str, args: Any, conversation_id: str = "adhoc") -> dict:
    kwargs = validate(name, args).model_dump()
    if name == "escalate_to_human":
        kwargs["conversation_id"] = conversation_id
    return getattr(store, name)(**kwargs)


def safe_execute(store: ClinicStore, name: str, args: Any, conversation_id: str = "adhoc") -> dict:
    """{'ok':True,'data':..} or {'ok':False,'error':..}. Nothing escapes as an exception."""
    try:
        return {"ok": True, "data": execute(store, name, args, conversation_id)}
    except ToolError as e:
        return e.to_dict()
    except Exception as e:  # noqa: BLE001 - last-resort containment, reported rather than hidden
        return {"ok": False, "error": {"code": "internal_error", "message": f"{type(e).__name__}: {e}"}}
