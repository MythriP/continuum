"""Analyst command line for the case workflow.

  reconcile init                   fresh start on day 1
  reconcile work [--oracle]        run every case that has something to do
  reconcile advance                move the clock to the next statement day
  reconcile board                  count of cases by status
  reconcile show CASE              one case: exception, finding, checks
  reconcile approve CASE --by NAME
  reconcile reject  CASE --by NAME --reason "..."
  reconcile correct CASE --by NAME --reason "..."
  reconcile audit CASE             the case's audit trail

`work` calls the real model (Gemini) unless --oracle is given.
"""
import argparse
import json
from pathlib import Path

from reconcile import match as M
from reconcile import ops, store


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reconcile")
    ap.add_argument("--data", type=Path, default=M.DATA)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    w = sub.add_parser("work")
    w.add_argument("--oracle", action="store_true", help="scripted investigator, no model calls")
    w.add_argument("--only")
    sub.add_parser("advance")
    sub.add_parser("board")
    for name in ("show", "audit"):
        sub.add_parser(name).add_argument("case")
    for name in ("approve", "reject", "correct"):
        p = sub.add_parser(name)
        p.add_argument("case")
        p.add_argument("--by", required=True)
        p.add_argument("--reason")
    a = ap.parse_args(argv)

    if a.cmd == "init":
        print(f"day {ops.init(a.data)}  {ops.board()}")
    elif a.cmd == "work":
        model = None
        if a.oracle:
            from reconcile import oracle
            model = oracle.make(a.data)
        print(ops.work(model, a.data, only=a.only), ops.board())
    elif a.cmd == "advance":
        d = ops.advance(a.data)
        print(f"day {d}  {ops.board()}" if d else "no more statement days")
    elif a.cmd == "board":
        print(f"day {store.today()}")
        for status, n in sorted(ops.board().items()):
            print(f"  {status:<18} {n}")
    elif a.cmd == "show":
        c = store.get(a.case)
        if not c:
            print(f"no case {a.case}")
            return 1
        print(json.dumps({k: c[k] for k in ("case_id", "status", "opened_on", "attempts",
                                            "exception", "finding", "verify")}, indent=1))
        if d := store.decision(a.case):
            print("decision:", d["decision"], "by", d["by"], "-", d["reason"] or "")
    elif a.cmd == "audit":
        for r in store.trail(a.case):
            print(f"{r['sim_date'] or '':<10}  {r['actor']:<16} {r['event']:<20} {r['detail'] or ''}")
    else:
        try:
            store.decide(a.case, a.cmd, a.by, a.reason)
        except ValueError as e:
            print(f"refused: {e}")
            return 2
        print(f"{a.cmd}: {a.case} by {a.by}. Run `work` to act on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
