"""Model access for every Continuum app: retry, quota handling, fallback, test model.

Each rule was learned from a real failure in an earlier application of the
framework:
  - a busy model (503) or a short rate limit is retried, waiting as long as the
    server asks
  - a daily quota wall is NOT retried: retrying only spends what is left
  - when one model stays busy, try the next one
  - a judge/reviewer model can be reserved so other agents never use up its quota
"""
import asyncio
import json
import os
import random
import re
from collections.abc import AsyncGenerator

from google.adk.agents import LlmAgent
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import InMemoryRunner
from google.genai import types

from continuum import config

config.load_env()

DEFAULT_MODEL = os.environ.get("CONTINUUM_MODEL", "gemini-3.5-flash")
FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.8-flash"]
TRANSIENT = ("429", "500", "502", "503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
             "DEADLINE_EXCEEDED", "high demand")
ATTEMPTS = 4


class QuotaExhausted(RuntimeError):
    """A daily quota is gone. Not retryable on this model."""


def quota_wall(msg: str) -> float | None:
    """Seconds until reset if this is a DAILY wall, else None.

    The metric name "free_tier_requests" appears in both per-minute and per-day
    errors, so only the PerDay quota id can tell them apart.
    """
    if "RESOURCE_EXHAUSTED" not in msg and "429" not in msg:
        return None
    if "PerDay" not in msg:
        return None
    m = re.search(r"retryDelay'?:\s*'?(\d+(?:\.\d+)?)s", msg)
    return float(m.group(1)) if m else 86400.0


def name_of(m) -> str:
    return m if isinstance(m, str) else getattr(m, "model", "")


# --------------------------------------------------------------- test model

class FakeLlm(BaseLlm):
    """Scripted model for tests. `reply(agent_name, prompt_text) -> dict`."""
    model: str = "fake"
    seen: list = []
    reply_fn: object = None

    async def generate_content_async(self, llm_request, stream=False) -> AsyncGenerator[LlmResponse, None]:
        text = "\n".join(p.text for c in (llm_request.contents or []) for p in (c.parts or [])
                         if getattr(p, "text", None))
        self.seen.append(text)
        out = self.reply_fn(text) if self.reply_fn else {}
        yield LlmResponse(content=types.Content(role="model",
                                                parts=[types.Part(text=json.dumps(out))]))


# --------------------------------------------------------------------- call

_JSON = re.compile(r"\{.*\}", re.S)

# Real model usage in this process, for cost reporting. Cache hits add nothing.
USAGE = {"model_requests": 0, "prompt_tokens": 0, "output_tokens": 0}


def _count(ev) -> None:
    u = getattr(ev, "usage_metadata", None)
    if u is None:
        return
    USAGE["model_requests"] += 1
    USAGE["prompt_tokens"] += u.prompt_token_count or 0
    USAGE["output_tokens"] += (u.candidates_token_count or 0) + (getattr(u, "thoughts_token_count", 0) or 0)


async def _once(agent: LlmAgent, payload: str) -> dict:
    runner = InMemoryRunner(agent=agent, app_name="continuum")
    s = await runner.session_service.create_session(app_name="continuum", user_id="u")
    out = ""
    async for ev in runner.run_async(user_id="u", session_id=s.id, new_message=types.Content(
            role="user", parts=[types.Part(text=payload)])):
        _count(ev)
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                if getattr(p, "text", None):
                    out = p.text          # keep the last text: the final answer
    m = _JSON.search(out)
    if m:
        return json.loads(m.group(0))
    # With tools AND an output schema, ADK can deliver the final answer through
    # its set_model_response step instead of as text. It is still saved under
    # the agent's output_key in session state, so read it from there.
    key = getattr(agent, "output_key", None)
    if key:
        sess = await runner.session_service.get_session(
            app_name="continuum", user_id="u", session_id=s.id)
        val = (sess.state or {}).get(key) if sess else None
        if isinstance(val, dict):
            return val
        if isinstance(val, str) and _JSON.search(val):
            return json.loads(_JSON.search(val).group(0))
    raise ValueError(f"{agent.name} returned no JSON: {out[:300]!r}")


async def ask_pinned(agent: LlmAgent, payload: str) -> dict:
    """One model, with retry. Raises QuotaExhausted on a daily wall."""
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return await _once(agent, payload)
        except Exception as e:                       # noqa: BLE001
            msg = str(e)
            last = e
            if quota_wall(msg) is not None:
                raise QuotaExhausted(f"{agent.name}: {name_of(agent.model)} daily quota gone") from e
            if not any(t in msg for t in TRANSIENT) or attempt == ATTEMPTS:
                raise
            delay = 3.0 * attempt * (0.75 + random.random() * 0.5)
            m = re.search(r"retry in (\d+(?:\.\d+)?)s", msg) or \
                re.search(r"retryDelay'?:\s*'?(\d+(?:\.\d+)?)s", msg)
            if m:
                delay = min(float(m.group(1)) + 1.0, 60.0)
            await asyncio.sleep(delay)
    raise last


async def ask(agent: LlmAgent, payload: str, *, reserved: set[str] = frozenset()) -> dict:
    """Retry, then fall back across models. `reserved` models are never used as
    fallbacks (keep the judge's quota for the judge)."""
    primary = name_of(agent.model)
    chain = [agent.model]
    if primary != "fake":
        chain += [m for m in FALLBACK_MODELS if m != primary and m not in reserved]
    last = None
    for i, m in enumerate(chain):
        a = agent if i == 0 else agent.model_copy(update={"model": m})
        try:
            return await ask_pinned(a, payload)
        except QuotaExhausted as e:
            last = e
        except Exception as e:                       # noqa: BLE001
            if not any(t in str(e) for t in TRANSIENT):
                raise
            last = e
    raise last
