"""
Short-window rate limiting per client key and per provider.

Why this exists next to the daily budget, and what each one is for:
  * `hooks/budget.py` answers "may this account spend X tokens today" -- it is
    accounting, UTC-day scoped, and its denial is a business decision.
  * this file answers "is this caller hammering the gateway right now" -- seconds,
    not days. Without it one client that retries in a tight loop turns into N upstream
    calls and N locked connections, and the failover ladder amplifies it.

Deliberately in-process (a sliding window in memory, guarded by a lock):
  * it is a traffic-shape guard, not a distributed quota. Persisting it in SQLite
    would make every request pay a write for a decision that only matters while the
    process is up;
  * restart resets it, which is correct -- after a restart nobody is hammering.

Denials never lock a connection and never reach the vendor: the request is refused
before anything is sent, the same admission point the budget uses.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from engrix_router.storage import settings

_WINDOW_S = 60.0
_lock = threading.Lock()
_hits: dict[str, deque[float]] = {}


def _allowed(key: str, limit: int, now: float) -> bool:
    """Keep the last minute of hits for `key`. limit<=0 = unlimited (disabled)."""
    if limit <= 0:
        return True
    window = _hits.setdefault(key, deque())
    cutoff = now - _WINDOW_S
    while window and window[0] <= cutoff:
        window.popleft()
    if len(window) >= limit:
        return False
    window.append(now)
    return True


def retry_after(now: float) -> int:
    """Seconds until the oldest hit in the window leaves it (worst case: the window)."""
    ages = [now - hits[0] for hits in _hits.values() if hits]
    if not ages:
        return 0
    return max(1, int(_WINDOW_S - min(ages)) + 1)


def check_admission(*, api_key_id: str | None, provider: str) -> dict[str, Any]:
    """
    Refuse before sending. Returns {"allowed": bool, "blocked_by": [...]}.

    Two independent windows: one caller cannot exhaust a whole vendor's budget by
    itself, and one vendor being hot cannot block callers that use other vendors.
    """
    per_key = settings.get_int("ratelimit.requests_per_minute_per_key")
    per_provider = settings.get_int("ratelimit.requests_per_minute_per_provider")
    now = time.monotonic()
    blocked: list[dict[str, Any]] = []
    with _lock:
        if api_key_id and per_key > 0:
            if not _allowed(f"key:{api_key_id}", per_key, now):
                blocked.append({"scope": f"api_key:{api_key_id}", "limit": per_key,
                                "window_s": int(_WINDOW_S)})
        if provider and per_provider > 0:
            if not _allowed(f"provider:{provider}", per_provider, now):
                blocked.append({"scope": f"provider:{provider}", "limit": per_provider,
                                "window_s": int(_WINDOW_S)})
    return {"allowed": not blocked, "blocked_by": blocked,
            "retry_after_s": retry_after(now) if blocked else None}


def snapshot() -> dict[str, Any]:
    """What the dashboard shows: how much traffic each window is holding right now."""
    now = time.monotonic()
    with _lock:
        rows = []
        for scope, hits in _hits.items():
            fresh = [moment for moment in hits if now - moment <= _WINDOW_S]
            if fresh:
                rows.append({"scope": scope, "requests_last_minute": len(fresh)})
    return {"window_s": int(_WINDOW_S), "rows": sorted(rows)}


def reset() -> None:
    """Tests and the dashboard's 'clear' action: forget every window."""
    with _lock:
        _hits.clear()
