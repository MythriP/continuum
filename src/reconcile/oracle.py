"""A scripted investigator that answers from the generator's truth file.

For tests and for demos with no API key. It is a stand-in for the model, never
for the checks: every finding it produces still goes through verify.py.
"""
import json
import re
from pathlib import Path

from continuum import llm
from reconcile import match as M
from reconcile.data.generate import truth_for
from reconcile.evidence import Book


def make(data: Path, lie_on_decoys: bool = False) -> llm.FakeLlm:
    truth = json.loads((data / "truth.json").read_text())["breaks"]
    excs = M.exceptions(data)
    book = Book(data)

    def reply(text: str) -> dict:
        eid = re.search(r'"exception_id": "([^"]+)"', text).group(1)
        e = next(x for x in excs if x["exception_id"].endswith(eid) or x["exception_id"] == eid)
        b = truth_for(e, truth)
        if b["type"] == "true_error":
            if lie_on_decoys:
                L, C = book.ledger_by_id(e["ledger_ids"][0]), book.custody_by_id(e["custody_line_ids"][0])
                return {"cause": "commission_netted", "action": "adjust_ledger", "field": "commission",
                        "amount": round(float(C["cash_amount"]) - float(L["settle_amount"]), 2),
                        "ledger_id": L["ledger_id"], "explanation": "fee", "evidence": []}
            return {"cause": "unexplained", "action": "escalate", "explanation": "no cause reproduces it",
                    "evidence": []}
        r = b["resolution"]
        f = {"cause": b["type"], "second_cause": b.get("also"), "action": r["action"],
             "explanation": b["cause"], "evidence": []}
        if r["action"] == "adjust_ledger":
            f.update(field=r["field"], amount=r.get("amount"), value=r.get("value"),
                     ledger_id=b["ledger_ids"][0])
        elif r["action"] == "link_reference":
            f.update(ledger_id=b["ledger_ids"][0], custody_line_id=r["custody_line_id"])
        elif b["type"] == "partial_settlement":
            f["ledger_id"] = b["ledger_ids"][0]
        else:
            f["ledger_id"] = b["ledger_ids"][0]
        return f

    return llm.FakeLlm(seen=[], reply_fn=reply)
