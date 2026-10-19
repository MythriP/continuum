"""One case, end to end, as a resumable ADK agent.

  INVESTIGATE -> verified -> AWAIT_APPROVAL -> POST -> CLOSED
       |            pending  -> WAIT (re-investigate when the clock moves on)
       |            escalated-> ESCALATED (a human takes it; nothing posts)
       |            rejected -> retry once with the reason, then ESCALATED

A case STOPS CLEANLY at WAIT and AWAIT_APPROVAL without ending, and is picked
up later with the same invocation id. Nothing runs while it waits.

Posting is exactly-once on two levels: the postings table refuses a second
row for the same case, and a resumed run re-reads that row instead of trusting
its own memory of what it already did.
"""
import datetime as dt
import os
from collections.abc import AsyncGenerator
from enum import IntEnum
from typing import override

from google.adk.agents import BaseAgent
from google.adk.agents.base_agent import BaseAgentState
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event

from continuum import replay
from reconcile import store
from reconcile.agents import Finding, investigator, payload
from reconcile.evidence import Book
from reconcile.lessons import lessons_text
from reconcile.verify import verify

MAX_ATTEMPTS = 2
TERMINAL = {"CLOSED", "REJECTED", "CORRECTED", "ESCALATED", "MERGED"}
CRASH_AT = os.environ.get("RECON_CRASH_AT", "")     # e.g. "AFTER_POST", "INVESTIGATE"


class Step(IntEnum):
    INVESTIGATE = 1
    WAIT = 2
    APPROVE = 3
    POST = 4


class CaseState(BaseAgentState):
    step: int = int(Step.INVESTIGATE)


def _crash(point: str) -> None:
    if CRASH_AT == point:
        print(f"*** crash at {point} ***", flush=True)
        os._exit(9)


class CaseAgent(BaseAgent):
    model: object = None
    data: object = None

    @override
    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        case_id = ctx.session.state["case_id"]
        st = self._load_agent_state(ctx, CaseState) or CaseState()
        step = Step(st.step)
        book = Book(self.data) if self.data else Book()

        def advance(to: Step):
            ctx.set_agent_state(self.name, agent_state=CaseState(step=int(to)))
            return self._create_agent_state_event(ctx)

        def finish(status: str, detail=None):
            store.update(case_id, status=status)
            store.audit(case_id, "system", f"closed:{status}", detail)
            ctx.set_agent_state(self.name, end_of_agent=True)
            return self._create_agent_state_event(ctx)

        while True:
            case = store.get(case_id)
            today = store.today()

            # ------------------------------------------------ investigate / wait
            if step in (Step.INVESTIGATE, Step.WAIT):
                _crash("INVESTIGATE")
                exc = dict(case["exception"])
                as_of = max(dt.date.fromisoformat(exc["as_of"]), today)
                exc["as_of"] = as_of.isoformat()
                feedback = ""
                if case["verify"] and case["verify"]["status"] == "rejected":
                    feedback = (f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED BY THE CHECKS: "
                                f"{case['verify']['reason']}. Look again, or escalate.")
                agent = investigator(book, as_of, self.model, lessons=lessons_text())
                raw = await replay.ask(agent, payload(exc, book) + feedback,
                                       reserved={"gemini-3.8-flash"})
                finding = Finding(**raw).model_dump()
                v = verify(exc, finding, book, as_of)
                attempts = case["attempts"] + 1
                store.update(case_id, finding=finding, verify=v, attempts=attempts)
                store.audit(case_id, "investigator", "investigated",
                            {"cause": finding["cause"], "action": finding["action"],
                             "verify": v["status"], "reason": v["reason"], "as_of": exc["as_of"]},
                            key=f"{case_id}:investigated:{exc['as_of']}:{attempts}")

                if v["status"] == "escalated":
                    yield finish("ESCALATED", v["reason"])
                    return
                if v["status"] == "rejected":
                    if attempts >= MAX_ATTEMPTS:
                        yield finish("ESCALATED", f"checks rejected it twice: {v['reason']}")
                        return
                    continue                              # one more try, with the reason
                if v["status"] == "pending":
                    store.update(case_id, status="WAITING")
                    store.audit(case_id, "system", "waiting",
                                {"for": finding.get("waiting_for") or v["reason"]},
                                key=f"{case_id}:waiting:{exc['as_of']}")
                    if step != Step.WAIT:
                        yield advance(Step.WAIT)
                    return                                # stop; picked up when the clock moves
                step = Step.APPROVE
                store.update(case_id, status="AWAITING_APPROVAL")
                store.merge_overlapping(case_id, finding)  # one fix per set of lines
                yield advance(step)
                continue

            # --------------------------------------------------------- approval
            if step == Step.APPROVE:
                d = store.decision(case_id)
                if d is None:
                    store.audit(case_id, "system", "awaiting_approval",
                                key=f"{case_id}:awaiting_approval")
                    return                                # stop; an analyst decides
                if d["decision"] == "reject":
                    yield finish("REJECTED", d["reason"])
                    return
                if d["decision"] == "correct":
                    yield finish("CORRECTED", d["reason"])
                    return
                step = Step.POST
                yield advance(step)
                continue

            # ------------------------------------------------------------- post
            if step == Step.POST:
                d = store.decision(case_id)
                finding = case["finding"]
                posted = store.post(case_id, finding, d["by"])
                _crash("AFTER_POST")                      # the at-least-once window
                store.audit(case_id, "system", "posted",
                            {"amount": finding.get("amount"), "new": posted},
                            key=f"{case_id}:posted")
                yield finish("CLOSED", "posted")
                return
