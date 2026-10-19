"""The case workflow - wait, approve, post exactly once, survive a kill.

Uses the scripted oracle investigator: no network, no model cost. Kills run in
real subprocesses (os._exit) through the CLI, so nothing is faked in-process.
"""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from continuum import replay
from reconcile import ops, oracle, store
from reconcile.data import generate as G

ROOT = Path(__file__).parent.parent


@pytest.fixture
def world(tmp_path, monkeypatch):
    out = tmp_path / "gen"
    G.generate(seed=7, out=out)
    db = tmp_path / "var" / "reconcile.db"
    monkeypatch.setattr(store, "DB", db)
    monkeypatch.setattr(replay, "CACHE", tmp_path / "replay")
    truth = json.loads((out / "truth.json").read_text())["breaks"]
    return {"out": out, "db": db, "truth": truth}


def truth_type(world, case) -> str:
    return G.truth_for(case["exception"], world["truth"])["type"]


def cli(world, *args, crash_at: str = "") -> subprocess.CompletedProcess:
    env = {**os.environ, "RECON_DB": str(world["db"]), "RECON_CRASH_AT": crash_at}
    return subprocess.run([sys.executable, "-m", "reconcile.cli", "--data", str(world["out"]), *args],
                          cwd=ROOT, env=env, capture_output=True, text=True)


def approve_all(by="a.analyst") -> list[str]:
    ids = [c["case_id"] for c in store.cases("AWAITING_APPROVAL")]
    for i in ids:
        store.decide(i, "approve", by)
    return ids


# ------------------------------------------------------------------ happy path

def test_day_one_routes_every_case(world):
    model = oracle.make(world["out"])
    ops.init(world["out"])
    ops.work(model, world["out"])
    cases = store.cases()
    assert cases
    for c in cases:
        t = truth_type(world, c)
        want = {"true_error": {"ESCALATED"}, "partial_settlement": {"WAITING"},
                "reference_mismatch": {"AWAITING_APPROVAL", "MERGED"}}.get(t, {"AWAITING_APPROVAL"})
        assert c["status"] in want, (c["case_id"], t, c["verify"])
    assert store.postings() == []                        # nothing posts without approval


def test_approve_then_post_once_each(world):
    model = oracle.make(world["out"])
    ops.init(world["out"])
    ops.work(model, world["out"])
    ids = approve_all()
    ops.work(model, world["out"])
    ops.work(model, world["out"])                        # a second pass must not re-post
    assert all(store.get(i)["status"] == "CLOSED" for i in ids)
    posted = [p["case_id"] for p in store.postings()]
    assert sorted(posted) == sorted(ids)
    assert all(p["approved_by"] == "a.analyst" for p in store.postings())


def test_partial_waits_then_resolves_when_late_leg_arrives(world):
    model = oracle.make(world["out"])
    ops.init(world["out"])
    ops.work(model, world["out"])
    waiting = [c["case_id"] for c in store.cases("WAITING")]
    assert waiting

    ops.work(model, world["out"])                        # same day: still nothing new
    assert all(store.get(i)["status"] == "WAITING" for i in waiting)

    while ops.advance(world["out"]):
        ops.work(model, world["out"])
    for i in waiting:
        c = store.get(i)
        assert c["status"] == "AWAITING_APPROVAL", (i, c["verify"])
        events = [r["event"] for r in store.trail(i)]
        assert "evidence_arrived" in events and "waiting" in events
    assert store.cases("WAITING") == []
    # late legs were linked to the open case, not opened as new cases
    assert not any(truth_type(world, c) == "partial_settlement" and c["case_id"] not in waiting
                   and c["exception"]["kind"] == "unmatched_custody" for c in store.cases())


def test_reject_and_correct_close_without_posting(world):
    model = oracle.make(world["out"])
    ops.init(world["out"])
    ops.work(model, world["out"])
    a, b = [c["case_id"] for c in store.cases("AWAITING_APPROVAL")][:2]
    store.decide(a, "reject", "a.analyst", reason="wrong fee schedule")
    store.decide(b, "correct", "a.analyst", reason="it is a settlement date issue")
    ops.work(model, world["out"])
    assert store.get(a)["status"] == "REJECTED"
    assert store.get(b)["status"] == "CORRECTED"
    assert store.postings() == []


# ------------------------------------------------------------------- controls

def test_maker_checker(world):
    ops.init(world["out"])
    ops.work(oracle.make(world["out"]), world["out"])
    cid = store.cases("AWAITING_APPROVAL")[0]["case_id"]
    for bot in ("agent", "Investigator", "", "system"):
        with pytest.raises(ValueError):
            store.decide(cid, "approve", bot)
    for d in ("reject", "correct"):
        with pytest.raises(ValueError, match="reason"):
            store.decide(cid, d, "a.analyst")
    decoy = store.cases("ESCALATED")[0]["case_id"]
    with pytest.raises(ValueError, match="not awaiting approval"):
        store.decide(decoy, "approve", "a.analyst")         # can't approve an escalated case
    store.decide(cid, "approve", "a.analyst")
    with pytest.raises(ValueError, match="already decided"):
        store.decide(cid, "reject", "someone", reason="x")


def test_audit_trail_is_append_only(world):
    ops.init(world["out"])
    c = sqlite3.connect(world["db"])
    assert c.execute("SELECT COUNT(*) FROM audit").fetchone()[0] > 0
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE audit SET actor='nobody'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM audit")


def test_a_lying_agent_never_reaches_approval(world):
    liar = oracle.make(world["out"], lie_on_decoys=True)
    ops.init(world["out"])
    while True:
        ops.work(liar, world["out"])
        if not ops.advance(world["out"]):
            break
    ops.work(liar, world["out"])
    decoys = [c for c in store.cases() if truth_type(world, c) == "true_error"]
    assert decoys
    for c in decoys:
        assert c["status"] == "ESCALATED", (c["case_id"], c["status"])
        assert c["attempts"] == 2                        # retried once with the reason, then gave up
    assert store.postings() == []


# ---------------------------------------------------------------- kill tests

def test_kill_mid_post_posts_exactly_once(world):
    assert cli(world, "init").returncode == 0
    assert cli(world, "work", "--oracle").returncode == 0
    cid = store.cases("AWAITING_APPROVAL")[0]["case_id"]
    assert cli(world, "approve", cid, "--by", "a.analyst").returncode == 0

    p = cli(world, "work", "--oracle", "--only", cid, crash_at="AFTER_POST")
    assert p.returncode == 9, p.stderr[-800:]            # died after the row, before closing
    assert len(store.postings()) == 1
    assert store.get(cid)["status"] == "AWAITING_APPROVAL"

    p = cli(world, "work", "--oracle", "--only", cid)    # resume
    assert p.returncode == 0, p.stderr[-800:]
    assert store.get(cid)["status"] == "CLOSED"
    assert [x["case_id"] for x in store.postings()] == [cid]
    events = [r["event"] for r in store.trail(cid)]
    assert events.count("posted") == 1
    assert events.count("investigated") == 1            # resume did not re-investigate


def test_kill_during_investigate_leaves_no_duplicates(world):
    assert cli(world, "init").returncode == 0
    p = cli(world, "work", "--oracle", crash_at="INVESTIGATE")
    assert p.returncode == 9, p.stderr[-800:]
    p = cli(world, "work", "--oracle")
    assert p.returncode == 0, p.stderr[-800:]
    assert "OPEN" not in ops.board()
    for c in store.cases():
        events = [r["event"] for r in store.trail(c["case_id"])]
        assert events.count("opened") == 1
        assert events.count("investigated") == 1, (c["case_id"], events)


def test_a_reference_mismatch_is_one_case_and_one_posting(world):
    """It shows up twice (ledger side, custody side); only one fix may post."""
    model = oracle.make(world["out"])
    ops.init(world["out"])
    while True:
        ops.work(model, world["out"])
        if not ops.advance(world["out"]):
            break
    approve_all()
    ops.work(model, world["out"])
    refs = [b for b in world["truth"] if b["type"] == "reference_mismatch"]
    assert refs
    for b in refs:
        lines = set(b["ledger_ids"]) | set(b["custody_line_ids"])
        mine = [c for c in store.cases()
                if lines & (set(c["exception"]["ledger_ids"]) | set(c["exception"]["custody_line_ids"]))]
        assert sorted(c["status"] for c in mine) == ["CLOSED", "MERGED"], (b["break_id"], mine)
    posted = [p["case_id"] for p in store.postings()]
    assert len(posted) == len(set(posted))
    closed = [c["case_id"] for c in store.cases("CLOSED")]
    assert sorted(posted) == sorted(closed)


def test_the_live_workflow_uses_promoted_lessons(world, monkeypatch):
    """A promoted lesson must reach the investigator in real case runs, not only in experiments."""
    from reconcile import case as case_mod
    seen, real = [], case_mod.investigator
    monkeypatch.setattr(case_mod, "lessons_text", lambda: "Check the commission first.")
    monkeypatch.setattr(case_mod, "investigator",
                        lambda *a, **k: seen.append(k.get("lessons")) or real(*a, **k))
    ops.init(world["out"])
    first = store.cases()[0]["case_id"]
    ops.work(oracle.make(world["out"]), world["out"], only=first)
    assert seen and all(s == "Check the commission first." for s in seen)
