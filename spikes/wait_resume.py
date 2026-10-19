"""Feasibility spike: can an ADK run stop to WAIT, end cleanly, and resume later?

Resuming after a crash is a known ADK capability. A reconciliation case also
has to stop on purpose - waiting for a custodian confirmation that
may take days - and continue when it arrives. This tests exactly that, with no
LLM involved.
"""
import asyncio
import json
import sqlite3
import sys
from collections.abc import AsyncGenerator
from enum import IntEnum
from pathlib import Path
from typing import override

from google.adk.agents import BaseAgent
from google.adk.agents.base_agent import BaseAgentState
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("var/spike")
DIR.mkdir(parents=True, exist_ok=True)
DB = DIR / "evidence.db"
SESSIONS = f"sqlite+aiosqlite:///{DIR / 'sessions.db'}"
MARK = DIR / "run.json"


class Step(IntEnum):
    INVESTIGATE = 1
    WAIT = 2
    RESOLVE = 3


class CaseState(BaseAgentState):
    step: int = int(Step.INVESTIGATE)


def evidence() -> str | None:
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS ev (case_id TEXT PRIMARY KEY, body TEXT)")
    r = c.execute("SELECT body FROM ev WHERE case_id='C1'").fetchone()
    c.close()
    return r[0] if r else None


def log(msg):
    with open(DIR / "log.txt", "a") as f:
        f.write(msg + "\n")
    print(msg, flush=True)


class CaseAgent(BaseAgent):
    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        st = self._load_agent_state(ctx, CaseState) or CaseState()
        step = Step(st.step)
        log(f"advance: at {step.name}")
        if step <= Step.INVESTIGATE:
            log("investigate: done")
            step = Step.WAIT
            ctx.set_agent_state(self.name, agent_state=CaseState(step=int(step)))
            yield self._create_agent_state_event(ctx)
        if step == Step.WAIT:
            if evidence() is None:
                log("wait: no confirmation yet - stopping cleanly")
                return                       # NOT end_of_agent: we will be back
            log("wait: confirmation arrived")
            step = Step.RESOLVE
            ctx.set_agent_state(self.name, agent_state=CaseState(step=int(step)))
            yield self._create_agent_state_event(ctx)
        log("resolve: done")
        ctx.set_agent_state(self.name, end_of_agent=True)
        yield self._create_agent_state_event(ctx)


def runner():
    app = App(name="spike_wait", root_agent=CaseAgent(name="case"),
              resumability_config=ResumabilityConfig(is_resumable=True))
    return Runner(app=app, session_service=DatabaseSessionService(db_url=SESSIONS))


async def start():
    r = runner()
    s = await r.session_service.create_session(app_name="spike_wait", user_id="u")
    async for ev in r.run_async(user_id="u", session_id=s.id,
                                new_message=types.Content(role="user", parts=[types.Part(text="go")])):
        if ev.invocation_id and not MARK.exists():
            MARK.write_text(json.dumps({"session_id": s.id, "invocation_id": ev.invocation_id}))


async def resume():
    m = json.loads(MARK.read_text())
    r = runner()
    async for _ in r.run_async(user_id="u", session_id=m["session_id"],
                               invocation_id=m["invocation_id"]):
        pass


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "start":
        asyncio.run(start())
    elif cmd == "resume":
        asyncio.run(resume())
    elif cmd == "arrive":
        c = sqlite3.connect(DB)
        c.execute("CREATE TABLE IF NOT EXISTS ev (case_id TEXT PRIMARY KEY, body TEXT)")
        c.execute("INSERT OR REPLACE INTO ev VALUES ('C1', 'custodian confirms fee 12.50')")
        c.commit()
        c.close()
        print("evidence arrived")
