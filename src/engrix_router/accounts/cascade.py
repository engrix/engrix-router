"""Shape-cascade circuit breaker for anti-abuse (ZCode 3012) storms.

Why this exists (2026-10-11 12:31 UTC incident): one 984K-token request hit
model_concurrency (3009), the client retried with the SAME payload, and the
router's failover ladder served it to 11+ different accounts one by one --
each got a 120s anti_abuse_shape lock within 81 seconds. The edge flags the
CLIENT (the payload), not the account, so "next account" is not a cure; it is
how the block spreads.

What it does: after N DISTINCT connections get 3012 for one model within a
short window, the whole model is paused in memory for a pause period. New
requests fail fast with all_locked semantics (retry_after = remaining pause)
instead of burning account after account. The pause is deliberately NOT
persisted: it is short, it must self-expire, and a process restart naturally
re-probes once. This complements model_locks (per-account, DB-backed) --
it never writes them.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ..storage.settings import get_int
from ..storage.sqlite import now_ms

_lock = threading.Lock()
# model -> deque[(ts_ms, connection_id)]
_hits: dict[str, deque[tuple[int, str]]] = {}
# model -> pause-until epoch ms
_paused_until: dict[str, int] = {}


def _reset() -> None:
    """Test hook: wipe all in-memory breaker state."""
    with _lock:
        _hits.clear()
        _paused_until.clear()


def note_anti_abuse(model: str, connection_id: str, *, ts: int | None = None) -> bool:
    """Record one 3012 hit for (model, connection).

    Returns True when this hit TRIPPED (or confirmed) a model pause. The
    connection_id itself is already locked by register_error; this counts
    distinct victims to decide whether the edge is rejecting the payload
    everywhere.
    """
    moment = now_ms() if ts is None else ts
    window = max(1_000, get_int("health.cascade_window_ms"))
    min_accounts = max(2, get_int("health.cascade_min_accounts"))
    tripped = False
    with _lock:
        dq = _hits.setdefault(model, deque())
        dq.append((moment, connection_id))
        cutoff = moment - window
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        distinct = {cid for _, cid in dq}
        if len(distinct) >= min_accounts:
            _paused_until[model] = max(_paused_until.get(model, 0), moment + get_int("health.cascade_pause_ms"))
            tripped = True
    return tripped


def is_paused(model: str, *, ts: int | None = None) -> tuple[bool, int]:
    """Is this model currently cascade-paused? Returns (paused, until_ms)."""
    moment = now_ms() if ts is None else ts
    with _lock:
        until = _paused_until.get(model, 0)
        if until > moment:
            return True, until
        # expired -> drop so the dict cannot grow unbounded across models
        if until:
            _paused_until.pop(model, None)
        return False, 0


def status(model: str, *, ts: int | None = None) -> dict[str, Any]:
    """Dashboard/log helper: current breaker view for one model."""
    moment = now_ms() if ts is None else ts
    paused, until = is_paused(model, ts=moment)
    with _lock:
        window = max(1_000, get_int("health.cascade_window_ms"))
        dq = _hits.get(model) or deque()
        distinct = {cid for t, cid in dq if t >= moment - window}
    return {"model": model, "paused": paused, "until_ms": until,
            "distinct_3012_accounts_in_window": len(distinct)}
