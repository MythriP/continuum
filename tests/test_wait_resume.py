"""Feasibility: a case can stop to wait, end cleanly, and resume later - in new processes."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _step(cmd, d):
    p = subprocess.run([sys.executable, "-m", "spikes.wait_resume", cmd, str(d)],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr[-800:]
    return [l for l in p.stdout.splitlines() if l and not l.startswith(("[", "/"))]


def test_wait_then_resume_on_evidence(tmp_path):
    out = _step("start", tmp_path)
    assert "wait: no confirmation yet - stopping cleanly" in out

    out = _step("resume", tmp_path)                       # still nothing
    assert out[0] == "advance: at WAIT"                   # did not redo investigate
    assert "wait: no confirmation yet - stopping cleanly" in out

    _step("arrive", tmp_path)
    out = _step("resume", tmp_path)
    assert out == ["advance: at WAIT", "wait: confirmation arrived", "resolve: done"]

    assert _step("resume", tmp_path) == []                # finished: resume is a no-op
