"""The harder breaks: each is checked against the tempting wrong answer.

Added because the first data set scored 100% with a real model, leaving the
learning evaluation nothing to improve.
"""
import json
from collections import Counter

import pytest

from reconcile import match as M
from reconcile import refdata as R
from reconcile.data import generate as G
from reconcile.evidence import Book
from reconcile.verify import verify


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen")
    G.generate(seed=7, out=out)
    truth = json.loads((out / "truth.json").read_text())["breaks"]
    excs = [{**e, "as_of": d} for d in M.statement_days(out) for e in M.match(*M.load(d, out))["exceptions"]]
    return {"book": Book(out), "truth": truth, "excs": excs}


def of(world, variant):
    bs = [b for b in world["truth"] if b.get("variant") == variant]
    assert bs, variant
    return bs


def exc_for(world, b, kind=None):
    ids = set(b["ledger_ids"]) | set(b["custody_line_ids"])
    return next(e for e in world["excs"] if ids & (set(e["ledger_ids"]) | set(e["custody_line_ids"]))
                and (kind is None or e["kind"] == kind))


def gap(world, b):
    L = world["book"].ledger_by_id(b["ledger_ids"][0])
    C = world["book"].custody_by_id(b["custody_line_ids"][0])
    return round(float(C["cash_amount"]) - float(L["settle_amount"]), 2)


def check(world, b, **f):
    e = exc_for(world, b)
    return verify(e, {"ledger_id": b["ledger_ids"][0], **f}, world["book"], e["as_of"])["status"]


def test_every_variant_is_present_and_stamp_duty_is_mostly_held_out(world):
    v = Counter(b.get("variant") for b in world["truth"] if b.get("variant"))
    assert set(v) == G.HARD
    days = Counter(b["day"] for b in of(world, "stamp_duty"))
    assert days[R.TRADE_DATES[-1].isoformat()] > len(of(world, "stamp_duty")) / 2


def test_two_causes_need_both(world):
    for b in of(world, "two_causes"):
        g = gap(world, b)
        assert check(world, b, cause="commission_netted", action="adjust_ledger",
                     field="commission", amount=g) == "rejected"
        assert check(world, b, cause="fx_rate_date", action="adjust_ledger",
                     field="fx_rate", amount=g) == "rejected"
        assert check(world, b, cause="commission_netted", second_cause="fx_rate_date",
                     action="adjust_ledger", field="commission", amount=g) == "verified"
        assert check(world, b, cause="commission_netted", second_cause="dividend_withholding",
                     action="adjust_ledger", field="commission", amount=g) == "rejected"


def test_ambiguous_reference_links_by_cash_not_by_reference(world):
    for b in of(world, "ambiguous_reference"):
        e = exc_for(world, b, kind="unmatched_custody")
        C = world["book"].custody_by_id(b["custody_line_ids"][0])
        owner = b["related_ledger_ids"][0]                # whose reference the line carries
        assert M.norm_ref(world["book"].ledger_by_id(owner)["trade_ref"]) == M.norm_ref(C["client_ref"])
        link = {"cause": "reference_mismatch", "action": "link_reference", "custody_line_id": C["line_id"]}
        assert verify(e, {**link, "ledger_id": owner}, world["book"], e["as_of"])["status"] == "rejected"
        assert verify(e, {**link, "ledger_id": b["ledger_ids"][0]}, world["book"], e["as_of"])["status"] == "verified"


def test_near_miss_fee_is_not_close_enough(world):
    for b in of(world, "near_miss_fee"):
        L = world["book"].ledger_by_id(b["ledger_ids"][0])
        sched = G.commission(L["broker"], int(L["quantity"]), float(L["gross_amount"]))
        sched = sched if L["side"] == "BUY" else -sched
        assert 0.01 < abs(gap(world, b) - sched) < 0.5      # really is a near miss
        for amount in (gap(world, b), sched):
            assert check(world, b, cause="commission_netted", action="adjust_ledger",
                         field="commission", amount=amount) == "rejected"


@pytest.mark.parametrize("variant", ["subtle_lot", "subtle_cent", "near_miss_fee"])
def test_subtle_decoys_have_no_benign_explanation(world, variant):
    for b in of(world, variant):
        assert b["type"] == "true_error" and not b["benign"]
        for cause, field in [("commission_netted", "commission"), ("fx_rate_date", "fx_rate"),
                             ("stamp_duty", "stamp_duty"), ("dividend_withholding", "withholding_tax")]:
            assert check(world, b, cause=cause, action="adjust_ledger", field=field,
                         amount=gap(world, b)) == "rejected", (b["break_id"], cause)


def test_stamp_duty_verifies_and_commission_does_not_explain_it(world):
    for b in of(world, "stamp_duty"):
        g = gap(world, b)
        assert check(world, b, cause="stamp_duty", action="adjust_ledger", field="stamp_duty",
                     amount=g) == "verified"
        assert check(world, b, cause="commission_netted", action="adjust_ledger", field="commission",
                     amount=g) == "rejected"


def test_clean_uk_buys_carry_stamp_duty_and_match(world):
    uk = {s["isin"] for s in R.SECURITIES if s["market"] == "UK"}
    dirty = {l for b in world["truth"] for l in b["ledger_ids"]}
    buys = [l for l in world["book"].ledger if l["isin"] in uk and l["side"] == "BUY"
            and l["type"] == "TRADE" and l["ledger_id"] not in dirty]
    assert buys and all(float(l["stamp_duty"]) > 0 for l in buys)
