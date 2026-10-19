"""The case store: cases, an append-only audit trail, decisions, postings, the clock.

SQLite, one file. Three rules are enforced by the database itself, not by
convention:
  - the audit trail cannot be edited or deleted (triggers refuse it)
  - a case posts at most once (UNIQUE case_id on postings)
  - an audit event recorded twice (ADK may re-run a step on resume) is ignored
"""
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

from continuum import config

DB = Path(os.environ.get("RECON_DB", config.VAR / "reconcile.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY CHECK (id = 1), today TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cases (
    case_id       TEXT PRIMARY KEY,
    exception     TEXT NOT NULL,
    status        TEXT NOT NULL,
    opened_on     TEXT NOT NULL,
    finding       TEXT,
    verify        TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    session_id    TEXT,
    invocation_id TEXT,
    linked_to     TEXT,
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT UNIQUE,
    case_id   TEXT NOT NULL,
    actor     TEXT NOT NULL,
    event     TEXT NOT NULL,
    detail    TEXT,
    sim_date  TEXT,
    at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
    BEGIN SELECT RAISE(ABORT, 'audit trail is append-only'); END;
CREATE TABLE IF NOT EXISTS decisions (
    case_id  TEXT PRIMARY KEY,
    decision TEXT NOT NULL,          -- approve | reject | correct
    by       TEXT NOT NULL,
    reason   TEXT,
    correction TEXT,
    at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS postings (
    posting_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL UNIQUE,
    ledger_id   TEXT,
    action      TEXT NOT NULL,
    field       TEXT,
    amount      REAL,
    value       TEXT,
    approved_by TEXT NOT NULL,
    sim_date    TEXT,
    at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def reset() -> None:
    for suffix in ("", "-wal", "-shm"):
        p = DB.with_name(DB.name + suffix)
        if p.exists():
            p.unlink()


# ------------------------------------------------------------------- clock

def today() -> dt.date:
    c = connect()
    try:
        r = c.execute("SELECT today FROM clock WHERE id=1").fetchone()
        return dt.date.fromisoformat(r["today"]) if r else None
    finally:
        c.close()


def set_today(d: dt.date) -> None:
    c = connect()
    try:
        c.execute("INSERT INTO clock (id, today) VALUES (1, ?) "
                  "ON CONFLICT(id) DO UPDATE SET today=excluded.today", (d.isoformat(),))
        c.commit()
    finally:
        c.close()


# ------------------------------------------------------------------- cases

def open_case(exc: dict) -> bool:
    c = connect()
    try:
        cur = c.execute("INSERT OR IGNORE INTO cases (case_id, exception, status, opened_on) "
                        "VALUES (?,?,?,?)", (exc["exception_id"], json.dumps(exc), "OPEN", exc["as_of"]))
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def get(case_id: str) -> dict | None:
    c = connect()
    try:
        r = c.execute("SELECT * FROM cases WHERE case_id=?", (case_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("exception", "finding", "verify"):
            d[k] = json.loads(d[k]) if d[k] else None
        return d
    finally:
        c.close()


def update(case_id: str, **fields) -> None:
    c = connect()
    try:
        sets = ", ".join(f"{k}=?" for k in fields) + ", updated_at=datetime('now')"
        vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        c.execute(f"UPDATE cases SET {sets} WHERE case_id=?", (*vals, case_id))
        c.commit()
    finally:
        c.close()


def cases(status: str | None = None) -> list[dict]:
    c = connect()
    try:
        ids = [r["case_id"] for r in c.execute(
            "SELECT case_id FROM cases" + (" WHERE status=?" if status else "") + " ORDER BY case_id",
            (status,) if status else ())]
    finally:
        c.close()
    return [get(i) for i in ids]


def merge_overlapping(case_id: str, finding: dict) -> list[str]:
    """One fix per set of lines. A reference mismatch shows up twice (the
    ledger side and the custody side); once one case has a checked fix, the
    other open, undecided case about the same lines is merged into it, so the
    analyst sees one case and one fix is posted."""
    me = get(case_id)["exception"]
    lines = set(me["ledger_ids"]) | set(me["custody_line_ids"]) | \
        {finding.get("ledger_id"), finding.get("custody_line_id")} - {None}
    merged = []
    for c in cases():
        if c["case_id"] == case_id or c["status"] in ("CLOSED", "REJECTED", "CORRECTED", "ESCALATED", "MERGED"):
            continue
        if decision(c["case_id"]):
            continue
        if lines & (set(c["exception"]["ledger_ids"]) | set(c["exception"]["custody_line_ids"])):
            update(c["case_id"], status="MERGED", linked_to=case_id)
            audit(c["case_id"], "system", "merged", {"into": case_id}, key=f"{c['case_id']}:merged")
            audit(case_id, "system", "absorbed", {"case": c["case_id"]}, key=f"{case_id}:absorbed:{c['case_id']}")
            merged.append(c["case_id"])
    return merged


# ------------------------------------------------------------------- audit

def audit(case_id: str, actor: str, event: str, detail=None, key: str | None = None) -> None:
    c = connect()
    try:
        d = today()
        c.execute("INSERT OR IGNORE INTO audit (event_key, case_id, actor, event, detail, sim_date) "
                  "VALUES (?,?,?,?,?,?)",
                  (key or f"{case_id}:{event}:{d}", case_id, actor, event,
                   json.dumps(detail) if detail is not None else None, d.isoformat() if d else None))
        c.commit()
    finally:
        c.close()


def trail(case_id: str) -> list[dict]:
    c = connect()
    try:
        return [dict(r) for r in c.execute(
            "SELECT actor, event, detail, sim_date, at FROM audit WHERE case_id=? ORDER BY id", (case_id,))]
    finally:
        c.close()


# --------------------------------------------------------------- decisions

def decide(case_id: str, decision: str, by: str, reason: str | None = None,
           correction: dict | None = None) -> None:
    if decision not in ("approve", "reject", "correct"):
        raise ValueError("decision must be approve, reject or correct")
    if not by or by.lower() in ("agent", "system", "investigator", "reviewer"):
        raise ValueError("maker-checker: a named human must decide, not an agent")
    if decision in ("reject", "correct") and not reason:
        raise ValueError(f"{decision} needs a reason")
    case = get(case_id)
    if not case or case["status"] != "AWAITING_APPROVAL":
        raise ValueError(f"{case_id} is not awaiting approval "
                         f"({case['status'] if case else 'no such case'})")
    c = connect()
    try:
        prior = c.execute("SELECT decision, by FROM decisions WHERE case_id=?", (case_id,)).fetchone()
        if prior:
            raise ValueError(f"{case_id} already decided: {prior['decision']} by {prior['by']}")
        c.execute("INSERT INTO decisions (case_id, decision, by, reason, correction) VALUES (?,?,?,?,?)",
                  (case_id, decision, by, reason, json.dumps(correction) if correction else None))
        c.commit()
    finally:
        c.close()
    audit(case_id, f"analyst:{by}", f"decision:{decision}", {"reason": reason, "correction": correction})


def decision(case_id: str) -> dict | None:
    c = connect()
    try:
        r = c.execute("SELECT * FROM decisions WHERE case_id=?", (case_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


# ----------------------------------------------------------------- posting

def post(case_id: str, finding: dict, approved_by: str) -> bool:
    """Write the adjustment once. Returns False if this case already posted."""
    c = connect()
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO postings (case_id, ledger_id, action, field, amount, value,"
            " approved_by, sim_date) VALUES (?,?,?,?,?,?,?,?)",
            (case_id, finding.get("ledger_id"), finding["action"], finding.get("field"),
             finding.get("amount"), finding.get("value"), approved_by, today().isoformat()))
        c.commit()
        return cur.rowcount == 1
    finally:
        c.close()


def postings() -> list[dict]:
    c = connect()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM postings ORDER BY posting_id")]
    finally:
        c.close()
