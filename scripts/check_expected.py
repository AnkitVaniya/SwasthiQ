#!/usr/bin/env python3
"""Run every script in conversations/ and adversarial/ in-process and compare with its `expected` block."""
import json, pathlib, sys

root = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.agent import run_conversation  # noqa: E402

clinic = json.load(open(root / "backend/data/clinic.json", encoding="utf-8"))
bad = 0
for d in [a for a in sys.argv[1:] if not a.startswith("-")] or ["conversations", "adversarial"]:
    for p in sorted((root / d).glob("*.json")):
        s = json.load(open(p, encoding="utf-8")); e = s["expected"]
        r = run_conversation(clinic, s["id"], s["today"], s["turns"]).result
        names = [c["name"] for c in r["tool_calls"]]
        errs = []
        if r["terminal_state"] != e["terminal_state"]: errs.append(f"state {r['terminal_state']} != {e['terminal_state']}")
        if r["escalation_reason"] != e["escalation_reason"]: errs.append(f"reason {r['escalation_reason']} != {e['escalation_reason']}")
        errs += [f"missing call {n}" for n in e.get("must_call", []) if n not in names]
        errs += [f"forbidden call {n}" for n in e.get("must_not_call", []) if n in names]
        print("ok  " if not errs else "FAIL", s["id"], r["terminal_state"], r["escalation_reason"], r["patient_id"], r["appointment_id"], errs or "")
        if "-v" in sys.argv: print("      ", r["reply"])
        bad += bool(errs)
print("failures:", bad)
sys.exit(1 if bad else 0)
