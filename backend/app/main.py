"""REST API. POST /agent/run is the agent; /tools/* exposes the six tools directly against a shared clinic;
/api/* feeds the operator UI. The built React app is served from the same origin when present."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator

from .agent import RunOutput, run_conversation
from .logdb import LogDB
from .store import ClinicStore, ToolError
from .tools import TOOLS, execute

ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
CLINIC = json.load(open(os.environ.get("CLINIC_JSON", BACKEND / "data" / "clinic.json"), encoding="utf-8"))
DATA_DIR = pathlib.Path(os.environ.get("DATA_DIR", BACKEND / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT_DIRS = [ROOT / "conversations", ROOT / "adversarial"]
REPEATS = int(os.environ.get("DETERMINISM_RUNS", "3"))

LIVE = ClinicStore(CLINIC, str(DATA_DIR / "live.db"))  # the shared clinic behind /tools/*
LOG = LogDB(os.environ.get("LOG_DB", str(DATA_DIR / "app.db")))


def scripts() -> list[dict]:
    out = []
    for d in SCRIPT_DIRS:
        for p in sorted(d.glob("*.json")):
            s = json.load(open(p, encoding="utf-8"))
            out.append({"id": s["id"], "description": s.get("description", ""), "today": s["today"], "turns": s["turns"], "group": d.name})
    return out


@asynccontextmanager
async def lifespan(_: FastAPI):
    if LOG.count() == 0 and os.environ.get("SEED_DEMO", "1") == "1":
        for s in scripts():
            run_and_log(RunRequest(conversation_id=s["id"], today=s["today"], turns=s["turns"]), "seed")
    yield


app = FastAPI(title="Sunrise Clinic Front Desk Agent", version="1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class RunRequest(BaseModel):
    conversation_id: str
    today: str
    turns: list[str]

    @field_validator("today")
    @classmethod
    def _today(cls, v):
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("must be a real date formatted YYYY-MM-DD")
        return v

    @field_validator("conversation_id")
    @classmethod
    def _cid(cls, v):
        if not v.strip():
            raise ValueError("must not be empty")
        return v


@app.exception_handler(RequestValidationError)
async def _validation(_: Request, exc: RequestValidationError):
    problems = [{"field": ".".join(str(p) for p in e["loc"][1:]) or "body", "message": e["msg"]} for e in exc.errors()]
    return JSONResponse({"ok": False, "error": {"code": "invalid_request", "message": "Request body is invalid.", "problems": problems}}, 422)


def _fingerprint(out: RunOutput) -> str:
    r = {k: v for k, v in out.result.items() if k != "metrics"}  # everything except timing
    return json.dumps(r, sort_keys=True, ensure_ascii=False)


def run_and_log(req: RunRequest, source: str) -> dict:
    first = run_conversation(CLINIC, req.conversation_id, req.today, req.turns)
    prints = {_fingerprint(first)}
    for _ in range(REPEATS - 1):
        prints.add(_fingerprint(run_conversation(CLINIC, req.conversation_id, req.today, req.turns)))
    LOG.save(req.conversation_id, first, req.today, source, {"runs": REPEATS, "stable": len(prints) == 1})
    return first.result


@app.post("/agent/run")
def agent_run(req: RunRequest) -> dict:
    return run_and_log(req, "api")


@app.get("/health")
def health():
    return {"ok": True}


# ---- the six tools, against the shared clinic ------------------------------------------------------
@app.post("/tools/reset")
def tools_reset():
    LIVE.reset()
    return {"ok": True}


@app.get("/tools/state")
def tools_state():
    return {"today": LIVE.today, "appointments": LIVE.appointments()}


@app.post("/tools/{name}")
def tools_call(name: str, args: dict[str, Any]):
    if name not in TOOLS:
        return JSONResponse(ToolError("unknown_tool", f"Unknown tool '{name}'. Valid tools: {sorted(TOOLS)}.", "name", 404).to_dict(), 404)
    try:
        return {"ok": True, "data": execute(LIVE, name, args, "live")}
    except ToolError as e:
        return JSONResponse(e.to_dict(), e.status)


# ---- operator UI ------------------------------------------------------------------------------------------
def _summary(r) -> dict:
    return {"id": r["id"], "created_at": r["created_at"], "today": r["today"], "terminal_state": r["terminal_state"],
            "escalation_reason": r["escalation_reason"], "caller_said": r["caller_said"], "intent": r["intent"],
            "resolved": bool(r["resolved_at"]), "source": r["source"]}


@app.get("/api/stats")
def stats():
    rows = LOG.rows()
    esc = [r for r in rows if r["terminal_state"] == "escalated"]
    done = [r for r in rows if r["terminal_state"] in ("booked", "rescheduled", "cancelled")]
    open_ = [r for r in esc if not r["resolved_at"]]
    n = len(rows)
    return {"conversations": n, "completed": len(done), "completed_pct": round(100 * len(done) / n) if n else 0,
            "escalated": len(esc), "open": len(open_),
            "urgent_open": sum(1 for r in open_ if r["escalation_reason"] == "clinical_urgent")}


@app.get("/api/handoffs")
def handoffs(status: str = "open"):
    rows = [r for r in LOG.rows() if r["terminal_state"] == "escalated"]
    if status == "open":
        rows = [r for r in rows if not r["resolved_at"]]
    urgent = [r for r in rows if r["escalation_reason"] == "clinical_urgent"]
    rest = [r for r in rows if r["escalation_reason"] != "clinical_urgent"]
    newest = lambda r: (r["created_at"], r["id"])  # noqa: E731
    return [_summary(r) for r in sorted(urgent, key=newest, reverse=True) + sorted(rest, key=newest, reverse=True)]


def _not_found(cid: str):
    return HTTPException(404, {"ok": False, "error": {"code": "unknown_conversation", "message": f"No conversation '{cid}'."}})


@app.post("/api/handoffs/{cid}/resolve")
def resolve(cid: str):
    if not LOG.get(cid):
        raise _not_found(cid)
    if not LOG.resolve(cid):
        raise HTTPException(409, {"ok": False, "error": {"code": "not_open", "message": "Only an open escalated conversation can be resolved."}})
    return {"ok": True}


@app.get("/api/conversations")
def conversations():
    return [_summary(r) for r in LOG.rows()]


def _note(r, res: dict) -> dict:
    s = r["terminal_state"]
    if s == "escalated":
        wrote = [c for c in res["tool_calls"] if c["name"] in ("book_appointment", "reschedule_appointment", "cancel_appointment")]
        return {"tone": "danger", "text": "Handed to a human. No appointment was created, moved or cancelled." if not wrote else "Handed to a human."}
    if s in ("booked", "rescheduled", "cancelled"):
        return {"tone": "ok", "text": f"Appointment {res['appointment_id']} {s}."}
    return {"tone": "neutral", "text": "No action taken. No human needed." if s == "abandoned" else "Request declined. No human needed."}


@app.get("/api/conversations/{cid}")
def conversation(cid: str):
    r = LOG.get(cid)
    if not r:
        raise _not_found(cid)
    res = json.loads(r["result"])
    return {**_summary(r), "result": res, "transcript": json.loads(r["transcript"]), "determinism": json.loads(r["determinism"]),
            "note": _note(r, res), "clinic": f"{CLINIC['clinic']['name']}, {CLINIC['clinic']['city']}"}


@app.get("/api/scripts")
def api_scripts():
    return scripts()


# ---- built frontend (single-container deploy) -------------------------------------------------------------
DIST = pathlib.Path(os.environ.get("FRONTEND_DIST", ROOT / "frontend" / "dist"))
if DIST.is_dir():
    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (DIST / path).resolve()
        ok = path and DIST.resolve() in f.parents and f.is_file()  # never serve outside dist/
        return FileResponse(f if ok else DIST / "index.html")
