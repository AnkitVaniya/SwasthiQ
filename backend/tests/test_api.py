import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "LOG", main.LogDB(str(tmp_path / "log.db")))
    monkeypatch.setattr(main, "REPEATS", 2)
    return TestClient(main.app)


def post(client, cid, turns, today="2026-10-01"):
    return client.post("/agent/run", json={"conversation_id": cid, "today": today, "turns": turns})


def test_malformed_requests_are_422_not_500(client):
    for body in [{}, {"conversation_id": "x"}, {"conversation_id": "x", "today": "tomorrow", "turns": []}, {"conversation_id": "", "today": "2026-10-01", "turns": ["a"]},
                 {"conversation_id": "x", "today": "2026-10-01", "turns": "hello"}]:
        r = client.post("/agent/run", json=body)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


def test_handoff_queue_flow(client):
    assert post(client, "e1", ["Mujhe seene mein dard ho raha hai"]).json()["escalation_reason"] == "clinical_urgent"
    assert post(client, "e2", ["Ek goli le lun ya nahi? Dose kitni?"]).json()["escalation_reason"] == "medical_advice"
    assert post(client, "ok1", ["Dr. Rao ke saath 7 tareekh subah.", "Neha Bhatt, 9812200404"]).json()["terminal_state"] == "booked"
    hs = client.get("/api/handoffs").json()
    assert [h["id"] for h in hs] == ["e1", "e2"]  # urgent first
    assert client.get("/api/stats").json() == {"conversations": 3, "completed": 1, "completed_pct": 33, "escalated": 2, "open": 2, "urgent_open": 1}
    assert client.post("/api/handoffs/e1/resolve").status_code == 200
    assert client.post("/api/handoffs/e1/resolve").status_code == 409      # no double resolve
    assert client.post("/api/handoffs/ok1/resolve").status_code == 409     # only escalations have handoffs
    assert client.post("/api/handoffs/nope/resolve").status_code == 404
    assert [h["id"] for h in client.get("/api/handoffs").json()] == ["e2"]
    assert client.get("/api/stats").json()["urgent_open"] == 0


def test_conversation_detail(client):
    post(client, "d1", ["Dr. Rao ke saath 7 tareekh subah.", "Neha Bhatt, 9812200404"])
    d = client.get("/api/conversations/d1").json()
    assert d["determinism"] == {"runs": 2, "stable": True}
    kinds = [e["kind"] for e in d["transcript"]]
    assert kinds[0] == "caller" and kinds[-1] == "agent" and "tool" in kinds
    assert client.get("/api/conversations/missing").status_code == 404


def test_tools_http_shared_state_and_errors(client):
    client.post("/tools/reset")
    body = dict(patient_id="pt_0013", doctor_id="dr_rao", date="2026-10-03", start="09:00")
    assert client.post("/tools/book_appointment", json=body).status_code == 200
    r = client.post("/tools/book_appointment", json={**body, "patient_id": "pt_0014"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "slot_taken"
    assert client.post("/tools/nope", json={}).status_code == 404
    assert client.post("/tools/cancel_appointment", json={"appointment_id": "ap_0001"}).status_code == 422
    client.post("/tools/reset")
