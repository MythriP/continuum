"""Feasibility spike: one agent that calls tools AND returns structured output.

The investigator has to look evidence up (tools) and then return a typed
finding. Older ADK forbade output_schema with tools; 2.11 says it supports
both. This proves it on a real model before anything is built on it.
"""
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.runners import InMemoryRunner
from google.genai import types
from pydantic import BaseModel

load_dotenv(Path(__file__).parent.parent / ".env")
CALLS = []


def commission_for(trade_id: str) -> dict:
    """Look up the commission charged on a trade."""
    CALLS.append(("commission_for", trade_id))
    return {"trade_id": trade_id, "commission": 12.50, "currency": "USD"}


def ledger_amount(trade_id: str) -> dict:
    """The gross cash amount the internal ledger booked for a trade."""
    CALLS.append(("ledger_amount", trade_id))
    return {"trade_id": trade_id, "amount": 25012.50}


class Finding(BaseModel):
    cause: str
    difference: float
    evidence: list[str]


agent = LlmAgent(
    name="investigator", model="gemini-3.5-flash",
    instruction=("A trade's custodian cash amount is 25000.00. Use the tools to find "
                 "why it differs from the ledger. Report cause, the difference, and "
                 "which tool results are your evidence."),
    tools=[commission_for, ledger_amount], output_schema=Finding, output_key="finding")


async def main():
    r = InMemoryRunner(agent=agent, app_name="spike_tools")
    s = await r.session_service.create_session(app_name="spike_tools", user_id="u")
    final = None
    async for ev in r.run_async(user_id="u", session_id=s.id, new_message=types.Content(
            role="user", parts=[types.Part(text="Investigate trade HC-1042.")])):
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                if getattr(p, "text", None):
                    final = p.text
    print("tool calls:", CALLS)
    print("final:", final)
    f = Finding.model_validate_json(final)
    print("parsed OK:", f.model_dump())

asyncio.run(main())
