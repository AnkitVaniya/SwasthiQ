# Sunrise Clinic: Front Desk Agent

A Python REST API with a conversational front-desk agent over six tools, plus a React operator UI
(Handoff Queue, Conversation Detail, and two supporting screens). Synthetic data only.

```bash
docker compose up --build     # UI + API on http://localhost:8000
# or, without Docker (Python 3.10+, Node 18+):
make run
make test                     # 98 pytest tests
make eval                     # the official runner.py, 3x each, on all 23 scripts
```

The agent is a deterministic policy engine: **no API key, no network, no model.** `tokens` is reported as 0.

## Layout

| Path | Role |
|---|---|
| `backend/app/store.py` | The tool layer: SQLite, transactions, the six tools. Never calls a model. |
| `backend/app/tools.py` | Argument validation (specific, fixable errors) and dispatch |
| `backend/app/names.py` | Phonetic name matching (Imraan == Imran) |
| `backend/app/nlu.py` | Safety lexicons, Hinglish dates/times, names, intent. Pure functions. |
| `backend/app/agent.py` | Conversation policy: safety gate, then reads, then deferred writes |
| `backend/app/grounding.py` | Rejects any reply fact (time/date/id) not traceable to a tool call |
| `backend/app/main.py`, `logdb.py` | FastAPI app and operator-UI persistence |
| `frontend/` | React + Vite. `#/` Handoff Queue, `#/c/<id>` Conversation Detail, `#/conversations`, `#/try` |

## API contract

### `POST /agent/run`
Request `{"conversation_id": str, "today": "YYYY-MM-DD", "turns": [str]}` returns
`{conversation_id, tool_calls:[{name,arguments}], terminal_state, escalation_reason, patient_id, appointment_id, reply, metrics:{turns,tokens,latency_ms}}`.
Every run starts from `clinic.json` in a fresh in-memory database. "Today" comes from the request, never the clock.
Malformed bodies return `422 {"ok":false,"error":{"code":"invalid_request","problems":[{field,message}]}}`, never a 500.

### Six tools: `POST /tools/{name}` (shared persistent clinic; `POST /tools/reset` reseeds, `GET /tools/state` inspects)
| Tool | Arguments | Success data | Errors (code, HTTP) |
|---|---|---|---|
| `search_slots` | `doctor_id`, `date` | `available[]`, `closed_reason` (`holiday` / `doctor_on_leave` / `no_working_hours` / `fully_booked` / null) | `unknown_doctor` 404, `date_in_past` 422 |
| `book_appointment` | `patient_id`, `doctor_id`, `date`, `start` | the appointment | `slot_taken`, `invalid_slot`, `patient_conflict`, `holiday`, `doctor_on_leave`, `no_working_hours` (409); `unknown_patient` 404 |
| `reschedule_appointment` | `appointment_id`, `patient_id`, `doctor_id`, `date`, `start` | appointment + `previous` | `not_owner` 403, `same_slot`, `slot_taken`, `appointment_not_active` 409 |
| `cancel_appointment` | `appointment_id`, `patient_id` | appointment (`cancelled`) | `not_owner` 403, `already_cancelled` 409 |
| `lookup_patient` | any of `name`, `phone`, `dob`, `patient_id`; `include_appointments` | `candidates[]`, `match`, `resolved`, `verified`, `mismatch`, `wards`, `appointments` | `missing_identifier` 422 |
| `escalate_to_human` | `reason` (enum), `detail` | `handoff_id`, `status` | `invalid_reason` 422 |

Unknown tools, unknown/extra/missing arguments and malformed dates or times all return a specific 4xx with `code`, `field` and a message saying what was expected.
There is deliberately no bulk tool, so "cancel everything" has nothing to call.

### Operator UI API
`GET /api/stats`, `GET /api/handoffs`, `POST /api/handoffs/{id}/resolve`, `GET /api/conversations[/{id}]`, `GET /api/scripts`, `GET /health`.

## How data stays consistent on update

1. **Validate and write are one atomic step.** Every write runs inside `BEGIN IMMEDIATE`, which takes the write lock first, so there is no gap between "is the slot free?" and "take it".
2. **A storage-level guarantee.** A partial unique index on `(doctor_id, date, start) WHERE status='booked'` makes a double booking impossible even if the application checks were wrong; an index violation is reported as `slot_taken`.
   Tested by 8 threads racing for one slot (exactly one wins) and by a raw insert that bypasses the app (the index rejects it).
3. **Reschedule is one UPDATE in one transaction.** Ownership, status, target validity and conflicts are checked first; then a single statement moves the row, freeing the old slot and taking the new one together. Any failure rolls back and the original is untouched (tested across four failure kinds).
4. **Cancel is a status change, not a delete**, so ids are never reused and a second cancel is a clear `already_cancelled`.
5. **Ownership on every mutation:** `cancel` and `reschedule` take `patient_id` and refuse another patient's appointment (`not_owner`).
6. **A patient cannot hold two appointments at the same time** (`patient_conflict`).
7. **Overlapping working windows are merged** so one start time is one slot (Dr. Rao's Monday windows overlap at 11:45).
8. **Isolation:** each `/agent/run` gets its own database; nothing leaks between conversations.

## Why the agent is safe

The model-free design is the point: the model (if you add one) is free to say anything, so safety lives in code that it cannot talk around.
`agent.py` runs, in order: red-flag and medical-advice gate on the **raw** text of every turn, then injection stripping, then (only after the last turn clears) tool calls.
Nothing is written before the final turn passes the gate, so an emergency that arrives after every booking detail is complete cannot produce a booking.
Identity and authority come from tool results: a bare name never verifies; a shared phone never authorises; only `guardian_of` does.
Replies are templated from tool results and every time, date and id is checked by `grounding.py`; an ungrounded reply is replaced by a safe fallback and recorded in the transcript.

## Deploying

`Dockerfile` and `render.yaml` run the UI and API as one service on one origin (Render: New, Blueprint, point at the repo). The live link is whatever you deploy; I could not host it from here.
See `DECISIONS.md` for the data oddities I found, my choices, and known weaknesses.
