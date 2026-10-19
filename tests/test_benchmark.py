"""Benchmark plumbing: the investigate -> verify -> score loop, with a scripted model.

The scripted model answers with the oracle finding for each exception, or
with a deliberately wrong one, so scoring is checked without any network.
"""
import asyncio
import json

import pytest

from continuum import replay
from reconcile.data import generate as G
from reconcile.evals import benchmark


@pytest.fixture
def world(tmp_path, monkeypatch):
    out = tmp_path / "gen"
    G.generate(seed=7, out=out)
    monkeypatch.setattr(replay, "CACHE", tmp_path / "replay")
    truth = json.loads((out / "truth.json").read_text())["breaks"]
    return {"out": out, "truth": truth}


def fake_model(world, lie_on_decoys=False):
    from reconcile import oracle
    return oracle.make(world["out"], lie_on_decoys)


def test_oracle_model_scores_perfectly(world):
    rows = asyncio.run(benchmark.run(world["out"], model=fake_model(world)))
    s = benchmark.summarise(rows)
    assert s["errors"] == 0
    assert s["resolution_accuracy"] == 1.0, s
    assert s["decoys_escalated"] == s["decoys"] > 0
    assert s["false_explanations_passed"] == 0


def test_a_lying_model_is_caught_by_the_verifier(world):
    rows = asyncio.run(benchmark.run(world["out"], model=fake_model(world, lie_on_decoys=True)))
    s = benchmark.summarise(rows)
    assert s["false_explanations"] == s["decoys"] > 0
    assert s["outcomes"].get("false_explanation_caught") == s["decoys"]
    assert s["false_explanations_passed"] == 0
