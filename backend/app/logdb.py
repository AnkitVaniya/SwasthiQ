"""Operator-UI persistence: every conversation the agent handled, and whether a human has closed its handoff."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading

SCHEMA = """CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, created_at TEXT NOT NULL, today TEXT NOT NULL, source TEXT NOT NULL,
  terminal_state TEXT NOT NULL, escalation_reason TEXT, intent TEXT, caller_said TEXT,
  result TEXT NOT NULL, transcript TEXT NOT NULL, determinism TEXT NOT NULL, resolved_at TEXT)"""


class LogDB:
    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.execute(SCHEMA)
        self._c.commit()

    def save(self, cid: str, out, today: str, source: str, determinism: dict) -> None:
        r = out.result
        with self._lock:
            # re-running a conversation replaces it, and re-opens its handoff
            self._c.execute(
                "INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                (cid, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), today, source, r["terminal_state"],
                 r["escalation_reason"], out.intent, out.caller_said, json.dumps(r, ensure_ascii=False),
                 json.dumps(out.transcript, ensure_ascii=False), json.dumps(determinism)))
            self._c.commit()

    def rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._c.execute("SELECT * FROM conversations ORDER BY created_at DESC, id DESC").fetchall()

    def get(self, cid: str):
        with self._lock:
            return self._c.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()

    def count(self) -> int:
        with self._lock:
            return self._c.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]

    def resolve(self, cid: str) -> bool:
        """Close an OPEN escalated conversation. Returns False if it was not open (so a double click is a no-op, not a lie)."""
        with self._lock:
            cur = self._c.execute(
                "UPDATE conversations SET resolved_at=? WHERE id=? AND terminal_state='escalated' AND resolved_at IS NULL",
                (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), cid))
            self._c.commit()
            return cur.rowcount == 1
