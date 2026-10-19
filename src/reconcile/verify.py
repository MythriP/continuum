"""Check a proposed resolution in code, before any human or reviewer sees it.

A balance check alone is not enough: an adjustment equal to the gap always
"balances", so a made-up explanation would pass. Instead, for the cause the
agent claims, this recomputes what that cause would produce from independent
reference data (the commission schedule, the FX table, the calendar, the tax
table, the line history) and accepts only if it reproduces the break exactly.

A model judge can be persuaded by a plausible but false explanation, so
correctness is decided by code, never by another model.

Statuses:
  verified  the cause reproduces the break exactly; safe to send for approval
  pending   consistent so far, but evidence is still to come (a partial leg)
  escalated the agent declined to explain it; goes to a human, nothing posts
  rejected  the claimed cause does not reproduce the break
"""
import datetime as dt
import json
from pathlib import Path

from reconcile import match as M
from reconcile import refdata as R
from reconcile.data.generate import commission

TOL = json.loads((Path(__file__).parent / "policy.json").read_text())["auto_close_tolerance"]
SEC = {s["isin"]: s for s in R.SECURITIES}

EXPECTED_ACTION = {
    "commission_netted": ("adjust_ledger", "commission"),
    "fx_rate_date": ("adjust_ledger", "fx_rate"),
    "dividend_withholding": ("adjust_ledger", "withholding_tax"),
    "settlement_date": ("adjust_ledger", "settle_date"),
    "partial_settlement": ("await_and_merge", None),
    "duplicate_booking": ("cancel_ledger_line", None),
    "reference_mismatch": ("link_reference", None),
    "stamp_duty": ("adjust_ledger", "stamp_duty"),
}


def r2(x):
    return round(float(x) + 1e-9, 2)


def near(a, b, tol=TOL):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol + 1e-9


def _out(status, reason):
    return {"status": status, "ok": status != "rejected", "reason": reason}


def verify(exc: dict, f: dict, book, as_of: dt.date) -> dict:
    action, cause = f.get("action"), f.get("cause")
    if action == "escalate":
        return _out("escalated", "declined to explain; a human decides")
    if f.get("second_cause"):
        return _two_causes(exc, f, book, as_of)
    if cause not in EXPECTED_ACTION:
        return _out("rejected", f"unknown cause {cause!r}")
    want_action, want_field = EXPECTED_ACTION[cause]
    if action != want_action or (want_field and f.get("field") != want_field):
        return _out("rejected", f"{cause} needs {want_action}"
                    + (f" on {want_field}" if want_field else "")
                    + f", got {action} {f.get('field') or ''}".rstrip())

    L, C = _lines(exc, f, book)
    return CHECKS[cause](exc, f, L, C, book, as_of)


def _lines(exc, f, book):
    lid = f.get("ledger_id") or (exc["ledger_ids"][0] if exc["ledger_ids"] else None)
    cid = f.get("custody_line_id") or (exc["custody_line_ids"][0] if exc["custody_line_ids"] else None)
    return (book.ledger_by_id(lid) if lid else None), (book.custody_by_id(cid) if cid else None)


def _two_causes(exc, f, book, as_of):
    """Only one pair is supported: commission netted AND trade-date FX. Both
    must hold at once, and together reproduce the gap exactly."""
    pair = {f.get("cause"), f.get("second_cause")}
    if pair != {"commission_netted", "fx_rate_date"}:
        return _out("rejected", f"no known way for {sorted(pair, key=str)} to combine")
    if f.get("action") != "adjust_ledger" or f.get("field") not in ("commission", "fx_rate"):
        return _out("rejected", "two causes need adjust_ledger on commission or fx_rate")
    L, C = _lines(exc, f, book)
    if not (L and C):
        return _out("rejected", "needs the ledger line and the custody line")
    if L["type"] != "TRADE" or L["broker"] not in R.BROKERS or L["currency"] == L["settle_currency"]:
        return _out("rejected", "needs a brokered trade with a currency conversion")
    if float(L["commission"]) != 0.0:
        return _out("rejected", f"ledger already books a commission of {L['commission']}")
    td, sd = dt.date.fromisoformat(L["trade_date"]), dt.date.fromisoformat(L["settle_date"])
    if not near(L["settle_amount"], r2(float(L["net_amount"]) * R.fx(L["currency"], td))):
        return _out("rejected", "the ledger was not converted at the trade-date rate")
    c = commission(L["broker"], int(L["quantity"]), float(L["gross_amount"]))
    true_net = float(L["net_amount"]) + (c if L["side"] == "BUY" else -c)
    if not near(C["cash_amount"], r2(true_net * R.fx(L["currency"], sd))):
        return _out("rejected", "commission plus settle-date FX does not reproduce the custodian amount")
    want = r2(float(C["cash_amount"]) - float(L["settle_amount"]))
    if not near(f.get("amount"), want):
        return _out("rejected", f"adjustment {f.get('amount')} is not the combined difference {want}")
    return _out("verified", f"commission {c} {L['currency']} and trade- vs settle-date FX together explain {want}")


# ------------------------------------------------------------------ per cause

def _commission(exc, f, L, C, book, as_of):
    if not (L and C):
        return _out("rejected", "needs the ledger line and the custody line")
    if L["type"] != "TRADE" or L["broker"] not in R.BROKERS:
        return _out("rejected", "commission only applies to a brokered trade")
    if float(L["commission"]) != 0.0:
        return _out("rejected", f"ledger already books a commission of {L['commission']}")
    sd = dt.date.fromisoformat(L["settle_date"])
    c = commission(L["broker"], int(L["quantity"]), float(L["gross_amount"]))
    c = r2(c * R.fx(L["currency"], sd) / R.fx(L["settle_currency"], sd))
    signed = c if L["side"] == "BUY" else -c
    gap = r2(float(C["cash_amount"]) - float(L["settle_amount"]))
    if not near(gap, signed):
        return _out("rejected", f"schedule commission {signed} does not explain the gap {gap}")
    if not near(f.get("amount"), signed):
        return _out("rejected", f"adjustment {f.get('amount')} is not the schedule commission {signed}")
    return _out("verified", f"commission {signed} per {L['broker']} schedule explains the gap exactly")


def _fx(exc, f, L, C, book, as_of):
    if not (L and C):
        return _out("rejected", "needs the ledger line and the custody line")
    if L["currency"] == L["settle_currency"]:
        return _out("rejected", "no currency conversion on this trade")
    td, sd = dt.date.fromisoformat(L["trade_date"]), dt.date.fromisoformat(L["settle_date"])
    at_trade = r2(float(L["net_amount"]) * R.fx(L["currency"], td))
    at_settle = r2(float(L["net_amount"]) * R.fx(L["currency"], sd))
    if not near(L["settle_amount"], at_trade):
        return _out("rejected", "the ledger was not converted at the trade-date rate")
    if not near(C["cash_amount"], at_settle):
        return _out("rejected", "the custodian amount is not the settle-date conversion")
    want = r2(at_settle - at_trade)
    if not near(f.get("amount"), want):
        return _out("rejected", f"adjustment {f.get('amount')} is not the FX difference {want}")
    return _out("verified", f"trade-date vs settle-date {L['currency']}USD rate explains {want}")


def _withholding(exc, f, L, C, book, as_of):
    if not (L and C):
        return _out("rejected", "needs the ledger line and the custody line")
    if L["type"] != "DIVIDEND":
        return _out("rejected", "not a dividend")
    rate = R.WITHHOLDING[SEC[L["isin"]]["country"]]
    if rate == 0:
        return _out("rejected", "no withholding applies to this security")
    gross = float(L["gross_amount"])
    if not (near(L["settle_amount"], gross) and near(C["cash_amount"], r2(gross * (1 - rate)))):
        return _out("rejected", f"{rate:.0%} withholding does not reproduce these amounts")
    want = r2(float(C["cash_amount"]) - float(L["settle_amount"]))
    if not near(f.get("amount"), want):
        return _out("rejected", f"adjustment {f.get('amount')} is not the withholding {want}")
    return _out("verified", f"{rate:.0%} withholding on {gross} explains {want}")


def _settle_date(exc, f, L, C, book, as_of):
    if not (L and C):
        return _out("rejected", "needs the ledger line and the custody line")
    market = SEC[L["isin"]]["market"]
    right = R.settle_date(dt.date.fromisoformat(L["trade_date"]), market).isoformat()
    if C["value_date"] != right:
        return _out("rejected", f"custodian date {C['value_date']} is not the calendar date {right}")
    if L["settle_date"] == right:
        return _out("rejected", "the ledger date is already correct")
    if f.get("value") != right:
        return _out("rejected", f"proposed date {f.get('value')} is not the calendar date {right}")
    return _out("verified", f"{market} calendar gives {right}; ledger had {L['settle_date']}")


def _partial(exc, f, L, C, book, as_of):
    if not L:
        return _out("rejected", "needs the ledger line")
    legs = book.custody_by_ref(L["trade_ref"], as_of)
    if not legs:
        return _out("rejected", "no custody legs for this trade")
    q = sum(int(c["quantity"]) for c in legs)
    cash = sum(float(c["cash_amount"]) for c in legs)
    per = float(L["settle_amount"]) / int(L["quantity"])
    if any(not near(float(c["cash_amount"]), int(c["quantity"]) * per, TOL * 2) for c in legs):
        return _out("rejected", "a leg's cash does not match the trade's price per share")
    if q < int(L["quantity"]):
        return _out("pending", f"{q} of {L['quantity']} shares settled so far; waiting for the rest")
    if q == int(L["quantity"]) and near(cash, L["settle_amount"], TOL * len(legs)):
        return _out("verified", f"{len(legs)} legs add up to the full {q} shares and {r2(cash)}")
    return _out("rejected", f"legs add up to {q} shares, not {L['quantity']}")


def _duplicate(exc, f, L, C, book, as_of):
    if not L:
        return _out("rejected", "needs the ledger line to cancel")
    same = [o for o in book.ledger_by_ref(L["trade_ref"], as_of) if o["ledger_id"] != L["ledger_id"]]
    keys = ("fund", "isin", "side", "quantity", "price", "settle_amount", "settle_date")
    twins = [o for o in same if all(o[k] == L[k] for k in keys)]
    if not twins:
        return _out("rejected", "no identical twin booking exists")
    cus = book.custody_by_ref(L["trade_ref"], as_of)
    if len(cus) != 1:
        return _out("rejected", f"custodian shows {len(cus)} settlements, not one")
    return _out("verified", f"identical to {twins[0]['ledger_id']}; the custodian settled once")


def _reference(exc, f, L, C, book, as_of):
    if not (L and C):
        return _out("rejected", "needs both the ledger line and the custody line to link")
    if M.ACCOUNT_TO_FUND[C["account"]] != L["fund"] or C["isin"] != L["isin"]:
        return _out("rejected", "different fund or security")
    if int(C["quantity"]) != int(L["quantity"]) or not near(C["cash_amount"], L["settle_amount"]):
        return _out("rejected", "quantity or cash differs - not just a reference problem")
    if C["value_date"] != L["settle_date"]:
        return _out("rejected", "settlement dates differ")
    a, b = M.norm_ref(L["trade_ref"]), M.norm_ref(C["client_ref"])
    if a == b or sorted(a) != sorted(b):
        return _out("rejected", f"references {a} and {b} are not a transposition")
    rivals = [o for o in book.ledger if o["ledger_id"] != L["ledger_id"] and o["fund"] == L["fund"]
              and o["isin"] == L["isin"] and o["quantity"] == L["quantity"]
              and near(o["settle_amount"], C["cash_amount"]) and o["settle_date"] == C["value_date"]]
    if rivals:
        return _out("rejected", f"{rivals[0]['ledger_id']} fits this custody line equally well; a human must pick")
    return _out("verified", f"same trade; reference {b} is {a} with digits swapped")


def _stamp(exc, f, L, C, book, as_of):
    if not (L and C):
        return _out("rejected", "needs the ledger line and the custody line")
    market = SEC[L["isin"]]["market"]
    if L["type"] != "TRADE" or L["side"] != "BUY" or market not in R.STAMP_DUTY:
        return _out("rejected", "stamp duty only applies to UK purchases")
    if float(L["stamp_duty"] or 0) != 0.0:
        return _out("rejected", f"ledger already books stamp duty of {L['stamp_duty']}")
    sd = dt.date.fromisoformat(L["settle_date"])
    duty = r2(float(L["gross_amount"]) * R.STAMP_DUTY[market])
    want = r2((float(L["net_amount"]) + duty) * R.fx(L["currency"], sd) / R.fx(L["settle_currency"], sd))
    if not near(C["cash_amount"], want):
        return _out("rejected", f"{R.STAMP_DUTY[market]:.1%} stamp duty does not reproduce the custodian amount")
    gap = r2(float(C["cash_amount"]) - float(L["settle_amount"]))
    if not near(f.get("amount"), gap):
        return _out("rejected", f"adjustment {f.get('amount')} is not the stamp duty difference {gap}")
    return _out("verified", f"{R.STAMP_DUTY[market]:.1%} stamp duty {duty} {L['currency']} explains {gap}")


CHECKS = {"commission_netted": _commission, "fx_rate_date": _fx,
          "dividend_withholding": _withholding, "settlement_date": _settle_date,
          "partial_settlement": _partial, "duplicate_booking": _duplicate,
          "reference_mismatch": _reference, "stamp_duty": _stamp}
