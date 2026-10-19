"""Exact matching, in code, before any agent sees anything.

Ledger and custodian lines are paired on (fund, client reference, ISIN). The
custodian strips separators from references, so both sides are normalised
first - that is routine, not a break. A pair matches only if quantity, cash
amount (within the auto-close tolerance) and settlement date all agree.
Anything else becomes an exception with the facts the investigator needs.
"""
import csv
import datetime as dt
import json
import os
import re
from collections import defaultdict
from pathlib import Path

from continuum import config
from reconcile import refdata as R

DATA = Path(os.environ.get("RECONCILE_DATA", config.HOME / "data" / "reconcile"))
POLICY = json.loads((Path(__file__).parent / "policy.json").read_text())
ACCOUNT_TO_FUND = {acct: code for code, (_, acct, _) in R.FUNDS.items()}


def norm_ref(ref: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (ref or "").upper())


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def statement_days(data: Path = DATA) -> list[dt.date]:
    """Every date with a ledger or custodian file, including late statements."""
    out = set()
    for f in data.glob("*_*.csv"):
        out.add(dt.datetime.strptime(f.stem.split("_")[1], "%Y%m%d").date())
    return sorted(out)


def load(day: dt.date, data: Path = DATA) -> tuple[list[dict], list[dict]]:
    return (_read(data / f"ledger_{day:%Y%m%d}.csv"),
            _read(data / f"custody_{day:%Y%m%d}.csv"))


def _same(l: dict, c: dict, tol: float) -> dict:
    """-> the differences between one ledger and one custody line (empty = match)."""
    d = {}
    if int(l["quantity"]) != int(c["quantity"]):
        d["quantity"] = {"ledger": int(l["quantity"]), "custody": int(c["quantity"])}
    gap = round(float(c["cash_amount"]) - float(l["settle_amount"]), 2)
    if abs(gap) > tol:
        d["cash_amount"] = {"ledger": float(l["settle_amount"]),
                            "custody": float(c["cash_amount"]), "difference": gap}
    if l["settle_date"] != c["value_date"]:
        d["settle_date"] = {"ledger": l["settle_date"], "custody": c["value_date"]}
    return d


def match(ledger: list[dict], custody: list[dict], tol: float | None = None) -> dict:
    tol = POLICY["auto_close_tolerance"] if tol is None else tol
    L, C = defaultdict(list), defaultdict(list)
    for l in ledger:
        L[(l["fund"], norm_ref(l["trade_ref"]), l["isin"])].append(l)
    for c in custody:
        C[(ACCOUNT_TO_FUND[c["account"]], norm_ref(c["client_ref"]), c["isin"])].append(c)

    matched, exceptions = [], []

    def exc(kind, key, ls, cs, details=None):
        exceptions.append({
            "exception_id": f"X{len(exceptions) + 1:03d}", "kind": kind,
            "fund": key[0], "client_ref": key[1], "isin": key[2],
            "ledger_ids": [l["ledger_id"] for l in ls],
            "custody_line_ids": [c["line_id"] for c in cs],
            "details": details or {}})

    for key in sorted(set(L) | set(C)):
        ls, cs = L.get(key, []), C.get(key, [])
        if ls and not cs:
            for l in ls:
                exc("unmatched_ledger", key, [l], [])
        elif cs and not ls:
            for c in cs:
                exc("unmatched_custody", key, [], [c])
        elif len(ls) == 1 and len(cs) == 1:
            d = _same(ls[0], cs[0], tol)
            if d:
                exc("mismatch", key, ls, cs, d)
            else:
                matched.append((ls[0]["ledger_id"], cs[0]["line_id"]))
        else:
            # many-to-one or one-to-many: pair what matches exactly, flag the rest
            free = list(cs)
            for l in ls:
                hit = next((c for c in free if not _same(l, c, tol)), None)
                if hit:
                    matched.append((l["ledger_id"], hit["line_id"]))
                    free.remove(hit)
                else:
                    exc("extra_ledger_line" if len(ls) > len(cs) else "mismatch",
                        key, [l], free[:1],
                        _same(l, free[0], tol) if free else {})
                    free = free[1:]
            for c in free:
                exc("unmatched_custody", key, [], [c])
    return {"matched": matched, "exceptions": exceptions}


def exceptions(data: Path = DATA) -> list[dict]:
    out = []
    for d in statement_days(data):
        out += [{**e, "as_of": d.isoformat(), "exception_id": f"{d:%m%d}-{e['exception_id']}"}
                for e in match(*load(d, data))["exceptions"]]
    return out


if __name__ == "__main__":
    for day in statement_days():
        res = match(*load(day))
        kinds = defaultdict(int)
        for e in res["exceptions"]:
            kinds[e["kind"]] += 1
        print(f"{day}: {len(res['matched'])} matched, {len(res['exceptions'])} exceptions {dict(kinds)}")
