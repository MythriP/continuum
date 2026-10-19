"""What an investigator can look up. Nothing here reveals the true cause.

`Book` loads every ledger and custodian file. `tools_for(book, as_of)` returns
the functions an agent may call, limited to what was knowable on `as_of`: a
partial-settlement leg that arrives on a later statement stays invisible until
that statement's date. That is what forces a case to wait.
"""
import datetime as dt
from pathlib import Path

from reconcile import match as M
from reconcile import refdata as R
from reconcile.data.generate import commission as _commission

SEC = {s["isin"]: s for s in R.SECURITIES}


class Book:
    def __init__(self, data: Path = M.DATA):
        self.ledger, self.custody = [], []
        for d in M.statement_days(data):
            led, cus = M.load(d, data)
            self.ledger += [{**l, "_booked": d} for l in led]
            self.custody += [{**c, "_statement": d} for c in cus]

    def ledger_by_ref(self, ref, as_of):
        n = M.norm_ref(ref)
        return [l for l in self.ledger if M.norm_ref(l["trade_ref"]) == n and l["_booked"] <= as_of]

    def custody_by_ref(self, ref, as_of):
        n = M.norm_ref(ref)
        return [c for c in self.custody if M.norm_ref(c["client_ref"]) == n and c["_statement"] <= as_of]

    def ledger_by_id(self, lid):
        return next((l for l in self.ledger if l["ledger_id"] == lid), None)

    def custody_by_id(self, cid):
        return next((c for c in self.custody if c["line_id"] == cid), None)


def _clean(row: dict) -> dict:
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    for k in ("_booked", "_statement"):
        if k in row:
            out["statement_date" if k == "_statement" else "booked_on"] = row[k].isoformat()
    return out


def tools_for(book: Book, as_of: dt.date) -> list:
    """The investigator's toolbox, frozen at `as_of`."""

    def ledger_lines(client_ref: str) -> dict:
        """Every internal ledger line booked under this trade reference."""
        return {"lines": [_clean(l) for l in book.ledger_by_ref(client_ref, as_of)]}

    def custody_lines(client_ref: str) -> dict:
        """Every custodian statement line received so far for this client reference."""
        return {"as_of": as_of.isoformat(),
                "lines": [_clean(c) for c in book.custody_by_ref(client_ref, as_of)]}

    def ledger_search(fund: str, isin: str) -> dict:
        """Ledger lines for one security in one fund, any reference."""
        return {"lines": [_clean(l) for l in book.ledger
                          if l["fund"] == fund and l["isin"] == isin and l["_booked"] <= as_of]}

    def custody_search(fund: str, isin: str) -> dict:
        """Custodian lines received so far for one security in one fund, any reference."""
        acct = R.FUNDS[fund][1]
        return {"as_of": as_of.isoformat(),
                "lines": [_clean(c) for c in book.custody
                          if c["account"] == acct and c["isin"] == isin and c["_statement"] <= as_of]}

    def security(isin: str) -> dict:
        """Name, country, market, trading currency and dividend status of a security."""
        s = SEC.get(isin)
        return dict(s) if s else {"error": f"unknown ISIN {isin}"}

    def commission_schedule(broker: str, quantity: int, gross_amount: float) -> dict:
        """The commission this broker charges for a trade of this size, in the trade currency."""
        if broker not in R.BROKERS:
            return {"error": f"unknown broker {broker}"}
        return {"broker": broker, "commission": _commission(broker, int(quantity), float(gross_amount)),
                "rule": R.BROKERS[broker]}

    def fx_rate(currency: str, date: str) -> dict:
        """USD per one unit of `currency` on `date` (YYYY-MM-DD)."""
        try:
            return {"currency": currency, "date": date,
                    "usd_per_unit": R.fx(currency, dt.date.fromisoformat(date))}
        except KeyError:
            return {"error": f"no {currency} rate for {date}"}

    def market_calendar(market: str, trade_date: str) -> dict:
        """The correct settlement date for a trade, and the holidays near it. Market: US, EU or UK."""
        td = dt.date.fromisoformat(trade_date)
        near = sorted(d.isoformat() for d in R.HOLIDAYS[market] if abs((d - td).days) <= 10)
        return {"market": market, "trade_date": trade_date,
                "settlement_cycle": f"T+{R.SETTLEMENT_CYCLE[market]}",
                "settle_date": R.settle_date(td, market).isoformat(), "holidays_nearby": near}

    def withholding_rate(isin: str) -> dict:
        """Dividend withholding tax rate applied to this security for Harborline's funds."""
        s = SEC.get(isin)
        if not s:
            return {"error": f"unknown ISIN {isin}"}
        return {"isin": isin, "country": s["country"], "rate": R.WITHHOLDING[s["country"]]}

    return [ledger_lines, custody_lines, ledger_search, custody_search, security,
            commission_schedule, fx_rate, market_calendar, withholding_rate]
