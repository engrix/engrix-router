"""
Vendor quota: read, TTL cache, snapshot, history.

In 9router quota is read-only and on demand: /api/usage/[connectionId] hits the
upstream every time (app/api/usage/[connectionId]/route.js:133-150), caches per
process in RAM (sse/services/antigravityQuota.js:11-31), and its auto-ping
scheduler exists only for claude/codex (shared/services/quotaAutoPing.js:18-26).
Consequence: a restart forgets everything, there is no history, and "quota left
today" cannot be watched before it runs out.

Here: snapshots go into the `quota_snapshots` table, the TTL comes from settings
(`quota.ttl_s`), and a provider without a UsageSpec returns available=False -
never 0, never a guess.
"""
from __future__ import annotations

import json
import time
from typing import Any

from engrix_router.storage.sqlite import execute, now_ms, query, query_one
from engrix_router.core import logs as applog
from engrix_router.storage import settings
from engrix_router.core.types import Credentials, Provider, QuotaReading, UpstreamError


def _fresh(provider: str, connection_id: str, ttl_s: int) -> dict[str, Any] | None:
    row = query_one(
        "SELECT fetched_at FROM quota_snapshots WHERE provider = ? AND connection_id = ?"
        " ORDER BY fetched_at DESC LIMIT 1",
        (provider, connection_id),
    )
    if row is None:
        return None
    if now_ms() - int(row["fetched_at"]) > ttl_s * 1000:
        return None
    rows = query(
        "SELECT * FROM quota_snapshots WHERE provider = ? AND connection_id = ? AND fetched_at = ?",
        (provider, connection_id, int(row["fetched_at"])),
    )
    return {
        "cached": True,
        "fetched_at": int(row["fetched_at"]),
        "readings": [dict(item) for item in rows],
    }


def _store(provider: str, connection_id: str, readings: list[QuotaReading]) -> None:
    moment = now_ms()
    for reading in readings:
        execute(
            "INSERT INTO quota_snapshots(provider, connection_id, scope, used, total, remaining,"
            " remaining_pct, unlimited, reset_at, fetched_at, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (provider, connection_id, reading.scope, reading.used, reading.total, reading.remaining,
             reading.remaining_pct, 1 if reading.unlimited else 0, reading.reset_at_ms, moment,
             json.dumps(reading.raw, ensure_ascii=False, default=str)),
        )


def _pct(used: float | None, total: float | None, remaining: float | None) -> float | None:
    if total:
        if remaining is not None:
            return round(remaining / total * 100, 2)
        if used is not None:
            return round(max(0.0, (total - used)) / total * 100, 2)
    return None


async def get_quota(provider: Provider, creds: Credentials, *, force: bool = False) -> dict[str, Any]:
    """One connection -> a quota reading. Never raises; a failure becomes a message."""
    definition = provider.definition
    ttl = settings.get_int("quota.ttl_s")
    if not force:
        cached = _fresh(definition.id, creds.connection_id, ttl)
        if cached:
            return cached
    spec = definition.transport.usage
    if spec is None:
        return {"available": False, "reason": f"provider '{definition.id}' declares no usage spec",
                "readings": []}
    started = time.perf_counter()
    try:
        readings = await provider.fetch_quota(creds)
    except UpstreamError as exc:
        applog.warn(applog.NS_QUOTA, f"quota {definition.id}/{creds.name}: {exc}"[:300])
        return {"available": False, "reason": str(exc)[:400], "status": exc.status, "readings": []}
    except Exception as exc:  # pragma: no cover - jaringan tak terduga
        applog.error(applog.NS_QUOTA, f"quota {definition.id}/{creds.name} failed: {exc.__class__.__name__}")
        return {"available": False, "reason": f"{exc.__class__.__name__}: {exc}"[:400], "readings": []}
    normalized: list[QuotaReading] = []
    for reading in readings:
        reading.remaining_pct = _pct(reading.used, reading.total, reading.remaining)
        normalized.append(reading)
    if normalized:
        _store(definition.id, creds.connection_id, normalized)
    return {
        "available": bool(normalized),
        "cached": False,
        "fetched_at": now_ms(),
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "readings": [
            {
                "scope": item.scope, "used": item.used, "total": item.total,
                "remaining": item.remaining, "remaining_pct": item.remaining_pct,
                "unlimited": item.unlimited, "reset_at": item.reset_at_ms,
            }
            for item in normalized
        ],
    }


def latest_for_provider(provider: str) -> list[dict[str, Any]]:
    rows = query(
        "SELECT q.*, c.name AS connection_name FROM quota_snapshots q"
        " LEFT JOIN connections c ON c.id = q.connection_id"
        " WHERE q.provider = ? AND q.fetched_at = ("
        "   SELECT MAX(fetched_at) FROM quota_snapshots WHERE provider = ?)",
        (provider, provider),
    )
    return [dict(row) for row in rows]


def history(provider: str, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = query(
        "SELECT fetched_at, connection_id, scope, used, total, remaining, remaining_pct, reset_at"
        " FROM quota_snapshots WHERE provider = ? ORDER BY fetched_at DESC LIMIT ?",
        (provider, max(1, min(int(limit), 1000))),
    )
    return [dict(row) for row in rows]


def worst_remaining() -> dict[str, Any] | None:
    """The most critical row across each connection's newest snapshot, for the banner."""
    rows = query(
        "SELECT provider, connection_id, scope, remaining, remaining_pct, reset_at, fetched_at"
        " FROM quota_snapshots WHERE fetched_at = (SELECT MAX(fetched_at) FROM quota_snapshots)"
        " AND remaining_pct IS NOT NULL ORDER BY remaining_pct ASC LIMIT 1"
    )
    return dict(rows[0]) if rows else None
