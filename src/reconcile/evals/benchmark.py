"""Investigate every exception, verify each finding in code, score against the truth.

  python -m reconcile.evals.benchmark            run on the synthetic data
  python -m reconcile.evals.benchmark --report   re-score the last run (no model calls)
"""
import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

from continuum import config, replay
from reconcile import match as M
from reconcile.agents import Finding, investigator, payload
from reconcile.data.generate import truth_for
from reconcile.evidence import Book
from reconcile.verify import EXPECTED_ACTION, verify

VAR = config.VAR / "evals"


def score(exc, f, v, b) -> str:
    if b is None:
        return "no_truth"
    if not b["benign"]:
        if f["action"] == "escalate":
            return "decoy_escalated"
        return "false_explanation_caught" if v["status"] == "rejected" else "false_explanation_PASSED"
    if f["action"] == "escalate":
        return "missed_escalated"
    want_action = EXPECTED_ACTION[b["type"]][0]
    same_causes = {f["cause"], f.get("second_cause")} - {None} == {b["type"], b.get("also")} - {None}
    if same_causes and f["action"] == want_action and v["status"] in ("verified", "pending"):
        return "correct"
    return "wrong_caught" if v["status"] == "rejected" else "wrong_PASSED"


async def run(data: Path = M.DATA, model=None, concurrency: int = 4, cache_only=False) -> list[dict]:
    import datetime as dt
    book = Book(data)
    truth = json.loads((data / "truth.json").read_text())["breaks"]
    sem = asyncio.Semaphore(concurrency)

    async def one(exc):
        as_of = dt.date.fromisoformat(exc["as_of"])
        agent = investigator(book, as_of, model)
        try:
            async with sem:
                raw = await replay.ask(agent, payload(exc, book), cache_only=cache_only,
                                       reserved={"gemini-3.8-flash"})
            f = Finding(**raw).model_dump()
        except Exception as e:                      # noqa: BLE001
            return {"exception_id": exc["exception_id"], "error": f"{type(e).__name__}: {str(e)[:200]}"}
        v = verify(exc, f, book, as_of)
        b = truth_for(exc, truth)
        return {"exception_id": exc["exception_id"], "kind": exc["kind"], "as_of": exc["as_of"],
                "truth": b["type"] if b else None, "variant": b.get("variant") if b else None,
                "break_id": b["break_id"] if b else None,
                "finding": f, "verify": v, "outcome": score(exc, f, v, b)}

    return list(await asyncio.gather(*[one(e) for e in M.exceptions(data)]))


def summarise(rows):
    ok = [r for r in rows if "error" not in r]
    c = Counter(r["outcome"] for r in ok)
    benign = [r for r in ok if r["truth"] != "true_error"]
    decoys = [r for r in ok if r["truth"] == "true_error"]
    by_type = {}
    for r in ok:
        good = "decoy_escalated" if r["truth"] == "true_error" else "correct"
        t = by_type.setdefault(r.get("variant") or r["truth"], [0, 0])
        t[1] += 1
        t[0] += r["outcome"] == good
    return {
        "exceptions": len(rows), "errors": len(rows) - len(ok), "outcomes": dict(c),
        "resolution_accuracy": round(sum(r["outcome"] == "correct" for r in benign) / max(len(benign), 1), 3),
        "decoys": len(decoys),
        "decoys_escalated": sum(r["outcome"] == "decoy_escalated" for r in decoys),
        "false_explanations": sum(r["outcome"].startswith("false_explanation") for r in decoys),
        "false_explanations_passed": c.get("false_explanation_PASSED", 0) + c.get("wrong_PASSED", 0),
        "by_type": {k: f"{a}/{n}" for k, (a, n) in sorted(by_type.items())},
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args(argv)
    out = VAR / "benchmark.jsonl"
    rows = asyncio.run(run(cache_only=a.report))
    VAR.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(json.dumps(summarise(rows), indent=2))


if __name__ == "__main__":
    main()
