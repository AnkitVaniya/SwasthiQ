"""The tool layer's consistency guarantees. No agent, no language: just the six tools."""
import sqlite3
import threading

import pytest

from app.tools import safe_execute


def err(res):
    assert not res["ok"], res
    return res["error"]["code"]


def test_overlapping_windows_are_deduplicated(store):
    # Dr. Rao's Monday windows 09-12 and 11:45-15:00 overlap: 11:45 must be ONE slot
    res = store.search_slots("dr_rao", "2026-10-12")
    assert res["available"].count("11:45") == 1 and res["available"] == sorted(set(res["available"]))


@pytest.mark.parametrize("date,reason", [("2026-10-02", "holiday"), ("2026-10-04", "no_working_hours")])
def test_closed_days(store, date, reason):
    assert store.search_slots("dr_rao", date)["closed_reason"] == reason


def test_doctor_on_leave(store):
    assert store.search_slots("dr_sethi", "2026-10-06")["closed_reason"] == "doctor_on_leave"
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0001", doctor_id="dr_sethi", date="2026-10-06", start="10:00"))) == "doctor_on_leave"


def test_search_rejects_past_and_unknown(store):
    assert err(safe_execute(store, "search_slots", {"doctor_id": "dr_rao", "date": "2026-09-30"})) == "date_in_past"
    assert err(safe_execute(store, "search_slots", {"doctor_id": "dr_nobody", "date": "2026-10-03"})) == "unknown_doctor"


def test_book_then_slot_is_taken(store):
    a = store.book_appointment("pt_0013", "dr_rao", "2026-10-03", "09:00")
    assert a["status"] == "booked" and a["end"] == "09:15"
    assert "09:00" not in store.search_slots("dr_rao", "2026-10-03")["available"]
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0014", doctor_id="dr_rao", date="2026-10-03", start="09:00"))) == "slot_taken"


def test_existing_booking_cannot_be_double_booked(store):
    # cv_0015: 09:00 on 8 Oct is already booked in the data. The tool layer refuses even if asked.
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0027", doctor_id="dr_rao", date="2026-10-08", start="09:00"))) == "slot_taken"


def test_invalid_slot_and_patient_conflict(store):
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0013", doctor_id="dr_rao", date="2026-10-03", start="09:07"))) == "invalid_slot"
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0013", doctor_id="dr_rao", date="2026-10-03", start="20:00"))) == "invalid_slot"
    store.book_appointment("pt_0013", "dr_rao", "2026-10-03", "10:00")
    # same patient, same time, different doctor
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0013", doctor_id="dr_sethi", date="2026-10-03", start="10:00"))) == "patient_conflict"


def test_race_for_same_slot_exactly_one_wins(store):
    results, barrier = [], threading.Barrier(8)

    def go(pid):
        barrier.wait()
        results.append(safe_execute(store, "book_appointment", dict(patient_id=pid, doctor_id="dr_rao", date="2026-10-03", start="12:00")))
    ts = [threading.Thread(target=go, args=(f"pt_{i:04d}",)) for i in range(20, 28)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert sum(r["ok"] for r in results) == 1
    assert all(r["error"]["code"] == "slot_taken" for r in results if not r["ok"])
    assert sum(1 for a in store.appointments() if a["start"] == "12:00" and a["date"] == "2026-10-03" and a["status"] == "booked") == 1


def test_unique_index_is_an_independent_backstop(store):
    """Even a raw insert that skips every application check cannot create a double booking."""
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("INSERT INTO appointments(id,patient_id,doctor_id,date,start,end,status) VALUES ('ap_x','pt_0001','dr_rao','2026-10-08','09:00','09:15','booked')")


def test_reschedule_moves_one_row_and_frees_old_slot(store):
    before = len(store.appointments())
    r = store.reschedule_appointment("ap_0001", "pt_0001", "dr_rao", "2026-10-03", "10:00")
    assert r["previous"]["start"] == "09:30" and r["date"] == "2026-10-03" and len(store.appointments()) == before
    assert "09:30" in store.search_slots("dr_rao", "2026-10-01")["available"]


def test_failed_reschedule_changes_nothing(store):
    snapshot = store.appointments()
    for args in [dict(date="2026-10-08", start="09:00"),  # taken
                 dict(date="2026-10-02", start="10:00"),  # holiday
                 dict(date="2026-10-03", start="09:07"),  # not a slot
                 dict(date="2026-09-01", start="10:00")]:  # past
        assert not safe_execute(store, "reschedule_appointment", dict(appointment_id="ap_0001", patient_id="pt_0001", doctor_id="dr_rao", **args))["ok"]
    assert store.appointments() == snapshot


def test_reschedule_guards(store):
    assert err(safe_execute(store, "reschedule_appointment", dict(appointment_id="ap_0001", patient_id="pt_0002", doctor_id="dr_rao", date="2026-10-03", start="10:00"))) == "not_owner"
    assert err(safe_execute(store, "reschedule_appointment", dict(appointment_id="ap_0001", patient_id="pt_0001", doctor_id="dr_rao", date="2026-10-01", start="09:30"))) == "same_slot"
    store.cancel_appointment("ap_0001", "pt_0001")
    assert err(safe_execute(store, "reschedule_appointment", dict(appointment_id="ap_0001", patient_id="pt_0001", doctor_id="dr_rao", date="2026-10-03", start="10:00"))) == "appointment_not_active"


def test_cancel_is_a_status_change_and_ids_never_reused(store):
    store.cancel_appointment("ap_0001", "pt_0001")
    assert err(safe_execute(store, "cancel_appointment", dict(appointment_id="ap_0001", patient_id="pt_0001"))) == "already_cancelled"
    assert "09:30" in store.search_slots("dr_rao", "2026-10-01")["available"]
    assert len(store.appointments()) == 25  # still there, marked cancelled
    new = store.book_appointment("pt_0001", "dr_rao", "2026-10-03", "10:00")
    assert new["id"] == "ap_0026"


def test_cancel_requires_ownership(store):
    assert err(safe_execute(store, "cancel_appointment", dict(appointment_id="ap_0001", patient_id="pt_0002"))) == "not_owner"
    assert next(a for a in store.appointments() if a["id"] == "ap_0001")["status"] == "booked"


def test_lookup_name_phone_and_mismatch(store):
    d = store.lookup_patient(name="Sharma")
    assert len(d["candidates"]) == 3 and not d["resolved"]
    d = store.lookup_patient(name="Rajesh Kumar Sharma", phone="9812200011")
    assert d["match"]["patient_id"] == "pt_0001" and d["verified"]
    d = store.lookup_patient(name="Harpreet Kaur", phone="9812200311")
    assert d["mismatch"] and not d["candidates"]
    assert not store.lookup_patient(name="Priya Nair")["verified"]  # a bare name is not verification


def test_lookup_shared_phone_and_guardian(store):
    assert len(store.lookup_patient(phone="9812200166")["candidates"]) == 3
    d = store.lookup_patient(name="Sunita Gupta", phone="9812200166")
    assert d["match"]["patient_id"] == "pt_0008" and {w["patient_id"] for w in d["wards"]} == {"pt_0006", "pt_0007"}
    # guardian_of is one-directional
    assert store.lookup_patient(name="Kabir Joshi", phone="9812200197")["wards"] == []


def test_similar_names_are_not_merged_without_a_phone(store):
    assert len(store.lookup_patient(name="Imran Qureshi")["candidates"]) == 2
    assert store.lookup_patient(name="Imran Qureshi", phone="9812200228")["match"]["patient_id"] == "pt_0010"


def test_validation_errors_are_specific(store):
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0001", doctor_id="dr_rao", date="3 Oct", start="09:00"))) == "invalid_argument"
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0001", doctor_id="dr_rao", date="2026-10-03"))) == "missing_argument"
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_0001", doctor_id="dr_rao", date="2026-10-03", start="25:00"))) == "invalid_argument"
    assert err(safe_execute(store, "cancel_appointment", dict(appointment_id="ap_0001", patient_id="pt_0001", everything=True))) == "unknown_argument"
    assert err(safe_execute(store, "cancel_all_appointments", {})) == "unknown_tool"  # there is no bulk tool to call
    assert err(safe_execute(store, "lookup_patient", {})) == "missing_identifier"
    assert err(safe_execute(store, "escalate_to_human", {"reason": "because"})) == "invalid_reason"
    assert err(safe_execute(store, "book_appointment", dict(patient_id="pt_9999", doctor_id="dr_rao", date="2026-10-03", start="09:00"))) == "unknown_patient"


def test_escalate_records_a_handoff(store):
    r = safe_execute(store, "escalate_to_human", {"reason": "clinical_urgent", "detail": "x"}, "cv_x")
    assert r["ok"] and r["data"]["handoff_id"] == "ho_0001" and r["data"]["status"] == "open"
