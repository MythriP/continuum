"""Operations around cases: open a day, start or resume a case, move the clock."""
import asyncio
import datetime as dt
from pathlib import Path

from google.adk.apps import App, ResumabilityConfig
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from reconcile import match as M
from reconcile import store
from reconcile.case import TERMINAL, CaseAgent

APP, USER = "reconcile", "ops"


def _sessions() -> str:
    return f"sqlite+aiosqlite:///{store.DB.with_name('sessions.db')}"


def _runner(model, data) -> Runner:
    app = App(name=APP, root_agent=CaseAgent(name="case", model=model, data=data),
              resumability_config=ResumabilityConfig(is_resumable=True))
    return Runner(app=app, session_service=DatabaseSessionService(db_url=_sessions()))


# ------------------------------------------------------------------- the day

def days(data: Path = M.DATA) -> list[dt.date]:
    return M.statement_days(data)


def init(data: Path = M.DATA) -> dt.date:
    """Fresh start at the first statement day."""
    store.reset()
    sess = store.DB.with_name("sessions.db")
    for suffix in ("", "-wal", "-shm"):
        p = sess.with_name(sess.name + suffix)
        if p.exists():
            p.unlink()
    first = days(data)[0]
    store.set_today(first)
    open_day(first, data)
    return first


def open_day(day: dt.date, data: Path = M.DATA) -> dict:
    """Open a case per exception that day - except a late partial-settlement leg,
    which is not a new problem: it is news about a case that is already open."""
    opened, linked = [], []
    refs = {c["exception"]["client_ref"]: c["case_id"] for c in store.cases()}
    for e in M.exceptions(data):
        if e["as_of"] != day.isoformat():
            continue
        if e["kind"] == "unmatched_custody" and e["client_ref"] in refs:
            cid = refs[e["client_ref"]]
            store.audit(cid, "system", "evidence_arrived",
                        {"custody_line_ids": e["custody_line_ids"], "statement": day.isoformat()},
                        key=f"{cid}:evidence:{day}")
            linked.append(cid)
            continue
        if store.open_case(e):
            store.audit(e["exception_id"], "system", "opened", {"kind": e["kind"]},
                        key=f"{e['exception_id']}:opened")
            opened.append(e["exception_id"])
    return {"opened": opened, "linked": linked}


def advance(data: Path = M.DATA) -> dt.date | None:
    """Move the clock to the next statement day and open its cases."""
    ds = days(data)
    nxt = next((d for d in ds if d > store.today()), None)
    if nxt:
        store.set_today(nxt)
        open_day(nxt, data)
    return nxt


# --------------------------------------------------------------- run a case

async def _run(case_id: str, model, data, resume: bool) -> None:
    case = store.get(case_id)
    r = _runner(model, data)
    if not resume or not case["session_id"]:
        s = await r.session_service.create_session(app_name=APP, user_id=USER,
                                                   state={"case_id": case_id})
        store.update(case_id, session_id=s.id)
        inv = None
        async for ev in r.run_async(user_id=USER, session_id=s.id, new_message=types.Content(
                role="user", parts=[types.Part(text=case_id)])):
            if inv is None and ev.invocation_id:
                inv = ev.invocation_id
                store.update(case_id, invocation_id=inv)
    else:
        async for _ in r.run_async(user_id=USER, session_id=case["session_id"],
                                   invocation_id=case["invocation_id"]):
            pass


def work(model=None, data: Path = M.DATA, only: str | None = None) -> dict:
    """Run every case that has something to do. Safe to call repeatedly."""
    ran = {"started": 0, "resumed": 0, "idle": 0}
    for c in store.cases():
        if only and c["case_id"] != only:
            continue
        if c["status"] in TERMINAL:
            continue
        if c["status"] == "AWAITING_APPROVAL" and store.decision(c["case_id"]) is None:
            ran["idle"] += 1
            continue
        resume = c["status"] != "OPEN"
        asyncio.run(_run(c["case_id"], model, data, resume))
        ran["resumed" if resume else "started"] += 1
    return ran


def board() -> dict:
    out = {}
    for c in store.cases():
        out[c["status"]] = out.get(c["status"], 0) + 1
    return out
