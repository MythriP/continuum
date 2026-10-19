"""The code verifier accepts true causes and rejects plausible wrong ones.

The oracle fix for every injected break comes from truth.json. The wrong fixes
are the tempting ones: an adjustment equal to the gap, under a cause that does
not actually produce it. A balance check would pass those; this must not.
"""
import datetime as dt
import json

import pytest

from reconcile import match as M
from reconcile.data import generate as G
from reconcile.evidence import Book
from reconcile.verify import EXPECTED_ACTION, verify

BENIGN = list(EXPECTED_ACTION)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen")
    G.generate(seed=7, out=out)
    truth = json.loads((out / "truth.json").read_text())["breaks"]
    exceptions = []
    for d in M.statement_days(out):
        for e in M.match(*M.load(d, out))["exceptions"]:
            exceptions.append({**e, "as_of": d})
    return {"book": Book(out), "truth": truth, "exceptions": exceptions,
            "last": M.statement_days(out)[-1]}


def primary(world, b):
    """The exception that first surfaces this break."""
    ids = set(b["ledger_ids"]) | set(b["custody_line_ids"])
    hits = [e for e in world["exceptions"]
            if ids & (set(e["ledger_ids"]) | set(e["custody_line_ids"]))]
    return min(hits, key=lambda e: e["as_of"])


def oracle(b) -> dict:
    r = b["resolution"]
    f = {"cause": b["type"], "second_cause": b.get("also"), "action": r["action"],
         "ledger_id": b["ledger_ids"][0] if b["ledger_ids"] else None}
    if r["action"] == "adjust_ledger":
        f.update(field=r["field"], amount=r.get("amount"), value=r.get("value"))
    if r["action"] == "link_reference":
        f["custody_line_id"] = r["custody_line_id"]
    if b["type"] == "true_error":
        f = {"cause": "unexplained", "action": "escalate"}
    return f


def gap(world, e):
    L = world["book"].ledger_by_id(e["ledger_ids"][0]) if e["ledger_ids"] else None
    C = world["book"].custody_by_id(e["custody_line_ids"][0]) if e["custody_line_ids"] else None
    if L and C:
        return round(float(C["cash_amount"]) - float(L["settle_amount"]), 2)
    return None


# ---------------------------------------------------------------- true causes

def test_every_oracle_fix_is_accepted(world):
    for b in world["truth"]:
        e = primary(world, b)
        res = verify(e, oracle(b), world["book"], e["as_of"])
        assert res["ok"], (b["break_id"], b["type"], res)


def test_partial_settlement_waits_then_verifies(world):
    for b in [b for b in world["truth"] if b["type"] == "partial_settlement"]:
        e = primary(world, b)
        early = verify(e, oracle(b), world["book"], e["as_of"])
        late = verify(e, oracle(b), world["book"], world["last"])
        assert early["status"] == "pending", (b["break_id"], early)
        assert late["status"] == "verified", (b["break_id"], late)


def test_decoys_escalated_are_safe(world):
    for b in [b for b in world["truth"] if b["type"] == "true_error"]:
        e = primary(world, b)
        assert verify(e, oracle(b), world["book"], e["as_of"])["status"] == "escalated"


# ------------------------------------------------------- plausible wrong fixes

FIELD = {"commission_netted": "commission", "fx_rate_date": "fx_rate",
         "dividend_withholding": "withholding_tax", "stamp_duty": "stamp_duty"}


def test_no_benign_cause_explains_a_decoy(world):
    """The finance version of '3+ years': every cash cause, plugged with the
    exact gap so it would balance, must still be rejected on a real error."""
    for b in [b for b in world["truth"] if b["type"] == "true_error"]:
        e = primary(world, b)
        g = gap(world, e)
        for cause, field in FIELD.items():
            f = {"cause": cause, "action": "adjust_ledger", "field": field, "amount": g,
                 "ledger_id": e["ledger_ids"][0]}
            res = verify(e, f, world["book"], e["as_of"])
            assert res["status"] == "rejected", (b["break_id"], cause, res)


def test_the_wrong_cause_with_the_right_number_is_rejected(world):
    """A commission break 'explained' as FX, or withholding, with the exact gap."""
    for b in [b for b in world["truth"] if b["type"] in FIELD]:
        e = primary(world, b)
        g = gap(world, e)
        for cause, field in FIELD.items():
            if cause == b["type"]:
                continue
            f = {"cause": cause, "action": "adjust_ledger", "field": field, "amount": g,
                 "ledger_id": e["ledger_ids"][0]}
            assert verify(e, f, world["book"], e["as_of"])["status"] == "rejected", (b["type"], cause)


def test_the_right_cause_with_a_wrong_number_is_rejected(world):
    for b in [b for b in world["truth"] if b["type"] in FIELD]:
        e = primary(world, b)
        f = {**oracle(b), "amount": round(oracle(b)["amount"] + 1.00, 2)}
        assert verify(e, f, world["book"], e["as_of"])["status"] == "rejected", b["break_id"]


def test_mismatched_action_is_rejected(world):
    b = next(b for b in world["truth"] if b["type"] == "commission_netted")
    e = primary(world, b)
    f = {**oracle(b), "action": "cancel_ledger_line"}
    assert verify(e, f, world["book"], e["as_of"])["status"] == "rejected"


def test_wrong_settlement_date_is_rejected(world):
    for b in [b for b in world["truth"] if b["type"] == "settlement_date"]:
        e = primary(world, b)
        L = world["book"].ledger_by_id(b["ledger_ids"][0])
        right = dt.date.fromisoformat(b["resolution"]["value"])
        for wrong in (L["settle_date"], (right + dt.timedelta(days=1)).isoformat()):
            f = {**oracle(b), "value": wrong}
            assert verify(e, f, world["book"], e["as_of"])["status"] == "rejected", (b["break_id"], wrong)


def test_cancelling_a_line_with_no_twin_is_rejected(world):
    b = next(b for b in world["truth"] if b["type"] == "commission_netted")
    e = primary(world, b)
    f = {"cause": "duplicate_booking", "action": "cancel_ledger_line",
         "ledger_id": e["ledger_ids"][0]}
    assert verify(e, f, world["book"], e["as_of"])["status"] == "rejected"
