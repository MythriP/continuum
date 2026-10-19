"""The investigator: looks evidence up with tools, returns one typed finding."""
import json
from typing import Literal

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field

from continuum.llm import DEFAULT_MODEL
from reconcile.evidence import tools_for

Cause = Literal["commission_netted", "fx_rate_date", "settlement_date", "partial_settlement",
                "duplicate_booking", "reference_mismatch", "dividend_withholding", "stamp_duty",
                "unexplained"]
Action = Literal["adjust_ledger", "cancel_ledger_line", "link_reference", "await_and_merge",
                 "escalate"]


class Finding(BaseModel):
    cause: Cause
    action: Action
    explanation: str = Field(description="two sentences, plain English, for the analyst")
    evidence: list[str] = Field(description="the tool results that prove the cause")
    ledger_id: str | None = None
    custody_line_id: str | None = None
    second_cause: Cause | None = Field(None, description="only if two causes together explain it")
    field: Literal["commission", "fx_rate", "withholding_tax", "settle_date", "stamp_duty"] | None = None
    amount: float | None = Field(None, description="custodian minus ledger, signed, settle currency")
    value: str | None = Field(None, description="the corrected settlement date, YYYY-MM-DD")
    waiting_for: str | None = None


INVESTIGATOR = """You are a reconciliation analyst at Harborline Capital, a fictional asset manager.
Its internal ledger and its custodian, Meridian Trust & Custody, disagree about one item. Find out why.

Use the tools. Look at both sides before you decide.

Claim exactly one cause:
- commission_netted: ledger booked gross (commission 0); custodian settled net of the broker commission
- fx_rate_date: ledger converted at the trade-date FX rate; custodian at the settle-date rate
- settlement_date: the ledger settlement date is wrong for that market's calendar
- partial_settlement: the custodian settled only part of the shares so far, or this line is a later leg
- duplicate_booking: the same trade is booked twice in the ledger
- reference_mismatch: the same trade, but the custodian's client reference is mistyped
- dividend_withholding: ledger booked a dividend gross; custodian paid it net of withholding tax
- unexplained: nothing above reproduces the difference exactly

Rules - these are not negotiable:
1. Claim a cause ONLY if the tool evidence reproduces the difference exactly: to the cent, or the exact date.
   Never propose an adjustment just because it equals the gap. Closing a break with the wrong reason hides a real error.
2. If no cause reproduces it exactly, use cause "unexplained" and action "escalate". A wrong quantity or price
   in the ledger is a real booking error: escalate it. Do not explain it away.
3. Use the action that fits the cause:
   commission_netted / fx_rate_date / dividend_withholding -> adjust_ledger, with field commission / fx_rate / withholding_tax and amount
   settlement_date -> adjust_ledger, field settle_date, value = the correct date
   partial_settlement -> await_and_merge, with ledger_id; say in waiting_for what has not arrived yet
   duplicate_booking -> cancel_ledger_line, ledger_id = the duplicate to cancel
   reference_mismatch -> link_reference, with both ledger_id and custody_line_id
4. amount = custodian cash minus ledger settle_amount, signed, in the settlement currency.
   If two causes on one trade together reproduce it exactly (and neither does alone), put the
   second in second_cause; amount is the total.
5. In evidence, quote the tool results you relied on."""


def investigator(book, as_of, model=None, lessons: str = "") -> LlmAgent:
    """`lessons`: validated lessons from past cases. Empty = no memory,
    and the instruction is byte-identical to before, so old cache entries still hit."""
    instruction = INVESTIGATOR + (f"\n\nLESSONS FROM PAST CASES (checked on held-out data):\n{lessons.strip()}"
                                  if lessons.strip() else "")
    return LlmAgent(name="investigator", model=model or DEFAULT_MODEL,
                    instruction=instruction, tools=tools_for(book, as_of),
                    output_schema=Finding, output_key="finding",
                    disallow_transfer_to_parent=True, disallow_transfer_to_peers=True)


def payload(exc: dict, book) -> str:
    rows = {"ledger": [l for l in book.ledger if l["ledger_id"] in exc["ledger_ids"]],
            "custody": [c for c in book.custody if c["line_id"] in exc["custody_line_ids"]]}
    clean = {k: [{a: b for a, b in r.items() if not a.startswith("_")} for r in v]
             for k, v in rows.items()}
    return (f"TODAY IS {exc['as_of']}. Statements after today have not arrived.\n\n"
            f"EXCEPTION\n{json.dumps({k: v for k, v in exc.items() if k != 'as_of'}, indent=1)}\n\n"
            f"LINES\n{json.dumps(clean, indent=1)}")
