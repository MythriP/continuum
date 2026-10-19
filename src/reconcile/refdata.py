"""Reference data for Harborline Capital, a fictional asset manager.

Everything here is synthetic: the firm, its funds, the issuers, brokers,
custodian, prices, FX rates and fee schedules. Formats follow real conventions
so the data reads like real operations data:

- ISINs have a valid check digit (ISO 6166).
- US equities settle T+1 (the US moved to T+1 in May 2024); EU and UK
  equities settle T+2.
- US 2026 market holidays are the published NYSE schedule.
- UK dividends carry no withholding tax; the EU rate here is a synthetic
  treaty rate.
"""
import datetime as dt

FIRM = "Harborline Capital"
CUSTODIAN = "Meridian Trust & Custody"

# fund code -> (name, custodian account, settlement currency for foreign trades)
FUNDS = {
    "HLC-USLC": ("Harborline US Large Cap", "MTC-448210", "USD"),
    "HLC-USSC": ("Harborline US Small Cap", "MTC-448227", "USD"),
    "HLC-GLEQ": ("Harborline Global Equity", "MTC-448235", "USD"),
    "HLC-INC":  ("Harborline Income", "MTC-448243", "LOCAL"),
}

BROKERS = {
    # broker -> commission rule
    "Kestrel Securities": {"per_share": 0.004, "min": 5.00},
    "Alder & Finch":      {"per_share": 0.005, "min": 7.50},
    "Northgate Markets":  {"bps": 8.0, "min": 10.00},       # EU / UK desk
}

# --- securities: fictional issuers, valid-format ISINs ----------------------

def _isin_check_digit(payload: str) -> str:
    """ISO 6166: letters to numbers (A=10 ... Z=35), then Luhn."""
    digits = "".join(str(int(c, 36)) for c in payload)
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 0:              # rightmost payload digit is doubled
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return str((10 - total % 10) % 10)


def isin(country: str, body9: str) -> str:
    payload = country + body9
    return payload + _isin_check_digit(payload)


# (name, country, market, currency, price, pays_dividend)
_ISSUERS = [
    ("Northwind Industrial Corp", "US", "US", "USD", 142.10, True),
    ("Cobalt Ridge Software", "US", "US", "USD", 318.45, False),
    ("Bayfront Health Systems", "US", "US", "USD", 87.30, True),
    ("Silverline Logistics", "US", "US", "USD", 54.12, True),
    ("Quarry Point Materials", "US", "US", "USD", 23.88, False),
    ("Tidewater Energy Partners", "US", "US", "USD", 66.70, True),
    ("Lumen Grove Biotech", "US", "US", "USD", 12.45, False),
    ("Ironvale Rail", "US", "US", "USD", 198.60, True),
    ("Crescent Payments", "US", "US", "USD", 241.15, False),
    ("Pinecrest Consumer Brands", "US", "US", "USD", 77.95, True),
    ("Harlow Semiconductor", "US", "US", "USD", 405.20, False),
    ("Westmere Insurance Group", "US", "US", "USD", 61.40, True),
    ("Fernbrook Retail", "US", "US", "USD", 31.75, False),
    ("Granite Bay Utilities", "US", "US", "USD", 48.20, True),
    ("Skyreach Aerospace", "US", "US", "USD", 129.85, False),
    ("Oakhurst Data Centers", "US", "US", "USD", 92.60, False),
    ("Vasel Automobilwerke", "DE", "EU", "EUR", 88.40, True),
    ("Lindqvist Telecom", "SE", "EU", "EUR", 14.95, True),
    ("Maréchal Luxe", "FR", "EU", "EUR", 612.00, True),
    ("Brightwater Chemie", "DE", "EU", "EUR", 47.30, False),
    ("Corvo Energia", "IT", "EU", "EUR", 9.84, True),
    ("Ashcombe Banking Group", "GB", "UK", "GBP", 6.42, True),
    ("Thornley Pharmaceuticals", "GB", "UK", "GBP", 41.18, True),
    ("Kingsgate Infrastructure", "GB", "UK", "GBP", 12.07, False),
]


def _body(name: str, country: str) -> str:
    """Deterministic 9-character body from the issuer name."""
    import hashlib
    h = hashlib.sha1(f"{country}:{name}".encode()).hexdigest().upper()
    alnum = "".join(c for c in h if c.isalnum())
    return ("0" + alnum)[:9] if country == "US" else alnum[:9]


SECURITIES = [
    {"isin": isin(c, _body(n, c)), "name": n, "country": c, "market": m,
     "currency": cur, "price": p, "dividend": d}
    for n, c, m, cur, p, d in _ISSUERS
]

# --- calendars ---------------------------------------------------------------

US_HOLIDAYS_2026 = {dt.date(2026, m, d) for m, d in [
    (1, 1), (1, 19), (2, 16), (4, 3), (5, 25), (6, 19), (7, 3),
    (9, 7), (11, 26), (12, 25)]}
# EU (TARGET) and UK closures near the scenario window; weekends handled below
EU_HOLIDAYS_2026 = {dt.date(2026, 1, 1), dt.date(2026, 4, 3), dt.date(2026, 4, 6),
                    dt.date(2026, 5, 1), dt.date(2026, 12, 25), dt.date(2026, 12, 26)}
UK_HOLIDAYS_2026 = {dt.date(2026, 1, 1), dt.date(2026, 4, 3), dt.date(2026, 4, 6),
                    dt.date(2026, 5, 4), dt.date(2026, 5, 25), dt.date(2026, 8, 31),
                    dt.date(2026, 12, 25), dt.date(2026, 12, 28)}
HOLIDAYS = {"US": US_HOLIDAYS_2026, "EU": EU_HOLIDAYS_2026, "UK": UK_HOLIDAYS_2026}
SETTLEMENT_CYCLE = {"US": 1, "EU": 2, "UK": 2}


def is_business_day(d: dt.date, market: str) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS[market]


def add_business_days(d: dt.date, n: int, market: str) -> dt.date:
    out = d
    while n > 0:
        out += dt.timedelta(days=1)
        if is_business_day(out, market):
            n -= 1
    return out


def settle_date(trade_date: dt.date, market: str) -> dt.date:
    return add_business_days(trade_date, SETTLEMENT_CYCLE[market], market)


# --- FX (synthetic, USD per unit) -------------------------------------------

FX = {
    ("EUR", dt.date(2026, 11, 23)): 1.1612, ("GBP", dt.date(2026, 11, 23)): 1.3388,
    ("EUR", dt.date(2026, 11, 24)): 1.1597, ("GBP", dt.date(2026, 11, 24)): 1.3401,
    ("EUR", dt.date(2026, 11, 25)): 1.1634, ("GBP", dt.date(2026, 11, 25)): 1.3372,
    ("EUR", dt.date(2026, 11, 26)): 1.1649, ("GBP", dt.date(2026, 11, 26)): 1.3359,
    ("EUR", dt.date(2026, 11, 27)): 1.1621, ("GBP", dt.date(2026, 11, 27)): 1.3390,
    ("EUR", dt.date(2026, 11, 30)): 1.1588, ("GBP", dt.date(2026, 11, 30)): 1.3415,
    ("EUR", dt.date(2026, 12, 1)):  1.1603, ("GBP", dt.date(2026, 12, 1)):  1.3420,
}


def fx(ccy: str, d: dt.date) -> float:
    return 1.0 if ccy == "USD" else FX[(ccy, d)]


# --- withholding (synthetic treaty rates for a US fund) ----------------------

WITHHOLDING = {"US": 0.0, "DE": 0.15, "FR": 0.15, "IT": 0.15, "SE": 0.15, "GB": 0.0}

# --- UK stamp duty reserve tax: 0.5% of the consideration, on purchases only --
# (the real UK rate). Deliberately NOT in the investigator's prompt or tools:
# it is the break type the agent has never seen, for the learning test.

STAMP_DUTY = {"UK": 0.005}

# The three trade dates in the scenario: the week of US Thanksgiving 2026, so
# the holiday on Thu Nov 26 pushes T+1 settlement for Wednesday's US trades.
TRADE_DATES = [dt.date(2026, 11, 24), dt.date(2026, 11, 25), dt.date(2026, 11, 27)]
