"""Every model output, saved by a hash of its input.

Re-running or re-scoring costs nothing and gives identical numbers.
`cache_only=True` refuses to call the model at all - re-scoring must never
re-generate (in an earlier application a cache-key change once silently re-ran
ten paid calls).
"""
import hashlib
import json

from continuum import config, llm

CACHE = config.VAR / "replay"


class CacheMiss(RuntimeError):
    pass


def key(agent, payload: str, salt: str = "") -> str:
    h = hashlib.sha256()
    for part in (llm.name_of(agent.model), agent.name, str(agent.instruction), payload):
        h.update(part.encode())
        h.update(b"\0")
    if salt:                    # only a real salt touches the hash
        h.update(salt.encode())
        h.update(b"\0")
    return h.hexdigest()


async def ask(agent, payload: str, *, pinned: bool = False, salt: str = "",
              cache_only: bool = False, reserved=frozenset()) -> dict:
    if llm.name_of(agent.model) == "fake":      # scripted test models: never cache
        return await llm.ask_pinned(agent, payload)
    f = CACHE / f"{key(agent, payload, salt)}.json"
    if f.exists():
        return json.loads(f.read_text())
    if cache_only:
        raise CacheMiss(f"{agent.name}: not cached, refusing to call the model")
    out = await (llm.ask_pinned(agent, payload) if pinned
                 else llm.ask(agent, payload, reserved=reserved))
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out
