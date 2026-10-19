"""The synthetic data is sound, and matching catches every break and nothing else."""
import datetime as dt
import json

import pytest

from reconcile import match as M
from reconcile import refdata as R
from reconcile.data import generate as G


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen")
    G.generate(seed=7, out=out)
    truth = json.loads((out / "truth.json").read_text())["breaks"]
    flagged_l, flagged_c, exceptions = set(), set(), []
    for d in M.statement_days(out):
        res = M.match(*M.load(d, out))
        exceptions += res["exceptions"]
        for e in res["exceptions"]:
            flagged_l.update(e["ledger_ids"])
            flagged_c.update(e["custody_line_ids"])
    return {"out": out, "truth": truth, "exceptions": exceptions,
            "flagged_l": flagged_l, "flagged_c": flagged_c}


# ------------------------------------------------------------ reference data

def test_isin_check_digit_matches_a_published_isin():
    assert R.isin("US", "037833100") == "US0378331005"


def test_every_security_has_a_valid_isin():
    for s in R.SECURITIES:
        assert len(s["isin"]) == 12
        assert R.isin(s["isin"][:2], s["isin"][2:11]) == s["isin"]


def test_thanksgiving_pushes_us_t_plus_1():
    assert R.settle_date(dt.date(2026, 11, 25), "US") == dt.date(2026, 11, 27)
    assert R.settle_date(dt.date(2026, 11, 24), "US") == dt.date(2026, 11, 25)
    assert R.settle_date(dt.date(2026, 11, 27), "US") == dt.date(2026, 11, 30)


def test_eu_settles_t_plus_2():
    assert R.settle_date(dt.date(2026, 11, 24), "EU") == dt.date(2026, 11, 26)


# ------------------------------------------------------------------- generator

def test_generator_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    G.generate(seed=7, out=a)
    G.generate(seed=7, out=b)
    for f in a.iterdir():
        assert f.read_bytes() == (b / f.name).read_bytes(), f.name


def test_injected_break_counts(data):
    assert len(data["truth"]) == 53
    kinds = {b["type"] for b in data["truth"]}
    assert kinds == {"commission_netted", "fx_rate_date", "settlement_date",
                     "partial_settlement", "duplicate_booking", "reference_mismatch",
                     "dividend_withholding", "stamp_duty", "true_error"}


def test_a_duplicate_flags_only_the_copy(data):
    """The original trade matches cleanly; flagging it too would be a false break."""
    dups = [b for b in data["truth"] if b["type"] == "duplicate_booking"]
    assert dups
    for b in dups:
        assert b["ledger_ids"][0].endswith("X")
        assert b["related_ledger_ids"][0] not in data["flagged_l"]


def test_decoys_are_marked_not_benign(data):
    errors = [b for b in data["truth"] if b["type"] == "true_error"]
    assert errors and all(b["benign"] is False for b in errors)
    assert all(b["resolution"]["action"] == "escalate" for b in errors)


def test_late_legs_land_on_or_after_their_value_date(data):
    for b in data["truth"]:
        if b["type"] != "partial_settlement":
            continue
        late_id = b["resolution"]["late_line_id"]
        files = [d for d in M.statement_days(data["out"])
                 if any(r["line_id"] == late_id for r in M.load(d, data["out"])[1])]
        assert len(files) == 1
        assert files[0] >= dt.date.fromisoformat(b["resolution"]["late_value_date"])


# -------------------------------------------------------------------- matching

def test_every_break_is_flagged(data):
    """Recall: every line touched by an injected break is in some exception."""
    for b in data["truth"]:
        for lid in b["ledger_ids"]:
            assert lid in data["flagged_l"], (b["break_id"], b["type"], lid)
        for cid in b["custody_line_ids"]:
            assert cid in data["flagged_c"], (b["break_id"], b["type"], cid)


def test_nothing_clean_is_flagged(data):
    """Precision: no exception is made only of clean lines."""
    dirty_l = {l for b in data["truth"] for l in b["ledger_ids"]}
    dirty_c = {c for b in data["truth"] for c in b["custody_line_ids"]}
    for e in data["exceptions"]:
        touched = set(e["ledger_ids"]) | set(e["custody_line_ids"])
        assert touched & (dirty_l | dirty_c), f"false exception {e}"


def test_reference_formatting_alone_is_not_a_break():
    l = {"fund": "HLC-USLC", "trade_ref": "HLC-20261124-0001", "isin": "US0378331005",
         "quantity": "100", "settle_amount": "1000.00", "settle_date": "2026-11-25",
         "ledger_id": "L1"}
    c = {"account": "MTC-448210", "client_ref": "HLC202611240001", "isin": "US0378331005",
         "quantity": "100", "cash_amount": "1000.00", "value_date": "2026-11-25", "line_id": "C1"}
    assert M.match([l], [c])["exceptions"] == []


def test_sub_cent_rounding_auto_closes_but_a_cent_does_not():
    base = {"fund": "HLC-USLC", "trade_ref": "R1", "isin": "X", "quantity": "1",
            "settle_date": "2026-11-25", "ledger_id": "L1"}
    cus = {"account": "MTC-448210", "client_ref": "R1", "isin": "X", "quantity": "1",
           "value_date": "2026-11-25", "line_id": "C1"}
    ok = M.match([{**base, "settle_amount": "100.00"}], [{**cus, "cash_amount": "100.01"}])
    bad = M.match([{**base, "settle_amount": "100.00"}], [{**cus, "cash_amount": "100.02"}])
    assert ok["exceptions"] == [] and len(bad["exceptions"]) == 1
