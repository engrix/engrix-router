"""
Background services: the work nobody asked for in a request.

What lives here is a scheduler plus one real job (vendor quota sync). The two jobs
that used to be listed next to it are deliberately NOT implemented, and the reason is
evidence, not laziness:

  * token refresh -- no provider in this codebase has a verified refresh call. The
    Qoder device-token refresh was rejected by the vendor in our probes, and a PAT
    exchange has a documented route but an undocumented body. Writing a refresher now
    would mean inventing a request shape and calling it a feature.
  * periodic health probing -- the connection breaker already learns health from real
    traffic, and a probe that spends credits to find out what the next real request
    would have told us is a cost, not a signal (measured: 404 requests drained
    300 trial credits in under a day). A 0-credit probe exists: `provider.probe()`,
    reachable from the dashboard and `POST /api/connections/<id>/test`.

Why the scheduler is in-process asyncio and not cron/APScheduler: the gateway is one
process with one SQLite file, and a job that must not overlap itself is easier to
reason about when its lock is a local variable.
"""
from __future__ import annotations

import asyncio
from typing import Any

from engrix_router.accounts import connections, limits
from engrix_router.core import logs as applog
from engrix_router.core.config import UPSTREAM_TOTAL_TIMEOUT_S
from engrix_router.providers import registry
from engrix_router.storage import settings


async def sync_quota_once() -> dict[str, Any]:
    """
    Ask every quota-capable connection for its numbers, one at a time.

    Sequential on purpose: these are cheap GETs against the same vendor, and
    parallelising them makes one account's rate limit everyone's problem.
    """
    seen: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for definition in registry.definitions():
        if "usage" not in definition.features:
            continue
        for row in connections.list_for_provider(definition.id, include_health=False):
            creds = connections.load_credentials(row["id"])
            if creds is None or not creds.token:
                continue
            provider = registry.get_provider(definition.id)
            result = await limits.get_quota(provider, creds, force=True)
            rows.append({"provider": definition.id, "connection": row.get("name"),
                         "available": result.get("available"), "reason": result.get("reason")})
            seen.append({"connection_id": row["id"], "readings": len(result.get("readings") or [])})
    return {"synced": len(seen), "detail": seen, "rows": rows}


async def run_forever(*, stop: asyncio.Event, sleep_s=None) -> None:
    """Loop until `stop` is set. `sleep_s` exists so tests do not wait 10 minutes."""
    if sleep_s is None:
        sleep_s = settings.get_int("services.quota_sync_interval_s")
    while not stop.is_set():
        try:
            async with asyncio.timeout(UPSTREAM_TOTAL_TIMEOUT_S):
                await sync_quota_once()
        except Exception as exc:  # satu sinkron gagal gak boleh matikan loop-nya
            applog.warn(applog.NS_QUOTA, f"quota sync failed: {exc.__class__.__name__}: {exc}"[:300])
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(5, int(sleep_s)))
        except (asyncio.TimeoutError, TimeoutError):
            continue


def interval_setting() -> int:
    return settings.get_int("services.quota_sync_interval_s")
