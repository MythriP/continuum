"""Promoted lessons: what the investigator has learned from analyst corrections.

A lesson reaches the agent only after the promotion gate (evals/learning.py)
showed it helps on held-out data AND a human approved it. The gate's evidence is
stored next to each lesson as an HTML comment, which the agent never sees, so
the agent receives exactly the text that was tested.
"""
import datetime as dt
import re
from pathlib import Path

LESSONS = Path(__file__).parent / "lessons.md"


def lessons_text() -> str:
    """The lessons the investigator sees, with evidence comments removed."""
    if not LESSONS.exists():
        return ""
    return re.sub(r"<!--.*?-->\n?", "", LESSONS.read_text(), flags=re.S).strip()


def apply(lesson: str, evidence: str, *, approved: bool = False) -> Path:
    """Save a gated lesson. Refuses without a human's explicit approval."""
    if approved is not True:
        raise ValueError("saving a lesson needs a human's explicit approval")
    with open(LESSONS, "a", encoding="utf-8") as f:
        f.write(f"<!-- promoted {dt.date.today()}: {evidence} -->\n{lesson.strip()}\n\n")
    return LESSONS
