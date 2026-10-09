"""Read API: usage, request trace, quota, budget, logs.

Compared with 9router (11 /api/usage/* routes + a usageDaily rollup + byApiKey +
a 24h chart) this one is smaller on purpose: there is no multi-year chart that
needs caching here, and engrix-agent does not read these endpoints at all -- the
reader is the owner, through the dashboard. Kept: period, filters, pagination,
body redaction (9router's request-details route returns `{redacted:true}`,
src/app/api/usage/request-details/route.js:49-60 -- this gateway does the same).
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from engrix_router.api import deps
from engrix_router.storage.sqlite import date_key_utc, query, query_one
from engrix_router.core import logs as applog
from engrix_router.accounts import limits
from engrix_router.providers import registry
from engrix_router.hooks import budget
from engrix_router.subscribers import trace as trace_store

router = APIRouter(prefix="/api", tags=["observability"])

_PERIODS = {"today": 0, "24h": 1, "7d": 7, "30d": 30, "60d": 60, "all": 3650}


def _since_ms(period: str) -> int:
    if period not in _PERIODS:
        raise HTTPException(400, f"period must be one of {sorted(_PERIODS)}")
    import datetime
    import time

    if period == "today":
        # 'today' = sejak tengah malam UTC, bukan sejak sekarang. Reset kuota
        # vendor (kode 110) juga ikut ganti hari UTC, jadi
        # definisi "hari" harus sama di semua view.
        midnight = datetime.datetime.now(datetime.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return int(midnight.timestamp() * 1000)
    days = _PERIODS[period]
    return int((time.time() - days * 86400) * 1000)


@router.get("/usage/stats", dependencies=[Depends(deps.require_admin)])
async def usage_stats(period: str = Query(default="today")) -> dict[str, Any]:
    since = _since_ms(period)
    totals = query_one(
        "SELECT COUNT(*) AS requests, SUM(status='ok') AS ok, SUM(status!='ok') AS errors,"
        " COALESCE(SUM(prompt),0) AS prompt, COALESCE(SUM(completion),0) AS completion,"
        " COALESCE(SUM(cached),0) AS cached, COALESCE(SUM(total),0) AS tokens,"
        " COALESCE(SUM(cost_usd),0) AS cost FROM requests WHERE ts >= ?",
        (since,),
    )
    by_provider = query(
        "SELECT provider, COUNT(*) AS requests, COALESCE(SUM(total),0) AS tokens,"
        " COALESCE(SUM(cost_usd),0) AS cost, SUM(status!='ok') AS errors"
        " FROM requests WHERE ts >= ? GROUP BY provider ORDER BY tokens DESC",
        (since,),
    )
    by_model = query(
        "SELECT provider, model, COUNT(*) AS requests, COALESCE(SUM(total),0) AS tokens,"
        " COALESCE(SUM(cost_usd),0) AS cost FROM requests WHERE ts >= ?"
        " GROUP BY provider, model ORDER BY tokens DESC LIMIT 50",
        (since,),
    )
    by_key = query(
        "SELECT k.name AS key_name, r.api_key_id, COUNT(*) AS requests,"
        " COALESCE(SUM(r.total),0) AS tokens FROM requests r"
        " LEFT JOIN api_keys k ON k.id = r.api_key_id WHERE r.ts >= ?"
        " GROUP BY r.api_key_id ORDER BY tokens DESC",
        (since,),
    )
    by_connection = query(
        "SELECT c.name AS connection_name, r.connection_id, COUNT(*) AS requests,"
        " COALESCE(SUM(r.total),0) AS tokens, SUM(r.status!='ok') AS errors FROM requests r"
        " LEFT JOIN connections c ON c.id = r.connection_id WHERE r.ts >= ?"
        " GROUP BY r.connection_id ORDER BY requests DESC",
        (since,),
    )
    rollup = query(
        "SELECT date_key, provider, model, requests, ok, errors, prompt, completion, cached, cost_usd"
        " FROM usage_daily WHERE date_key >= ? ORDER BY date_key DESC LIMIT 400",
        (date_key_utc() if period == "today" else _date_floor(period),),
    )
    return {
        "period": period,
        "totals": dict(totals or {}),
        "by_provider": [dict(row) for row in by_provider],
        "by_model": [dict(row) for row in by_model],
        "by_key": [dict(row) for row in by_key],
        "by_connection": [dict(row) for row in by_connection],
        "daily_rollup": [dict(row) for row in rollup],
        "budget": budget.snapshot(),
    }


def _date_floor(period: str) -> str:
    import datetime

    days = _PERIODS[period]
    return (datetime.datetime.now(datetime.timezone.utc) -
            datetime.timedelta(days=days)).strftime("%Y-%m-%d")


@router.get("/usage/chart", dependencies=[Depends(deps.require_admin)])
async def usage_chart(period: str = Query(default="7d")) -> list[dict[str, Any]]:
    rows = query(
        "SELECT date_key AS label, SUM(requests) AS requests, SUM(prompt + completion) AS tokens,"
        " SUM(cost_usd) AS cost FROM usage_daily WHERE date_key >= ?"
        " GROUP BY date_key ORDER BY date_key",
        (_date_floor(period),),
    )
    return [dict(row) for row in rows]


@router.get("/usage/requests", dependencies=[Depends(deps.require_admin)])
async def list_requests(limit: int = Query(default=50, ge=1, le=500),
                        provider: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    return trace_store.recent(limit=limit, provider=provider, status=status)


@router.get("/usage/requests/{request_id}", dependencies=[Depends(deps.require_admin)])
async def request_detail(request_id: str, include_payload: bool = Query(default=True)) -> dict[str, Any]:
    """Per-step trace (client_in -> openai_mid -> provider_out -> upstream_in ->
    client_out). This replaces 9router's /api/translator/load plus its 7-file dump.

    Payload bodies are opt-in: on by default (for debugging), but they can be
    switched off so the dashboard can show a list without exposing the
    conversation contents.
    """
    item = trace_store.get(request_id)
    if item is None:
        raise HTTPException(404, "request not found (probably rotated out)")
    if not include_payload:
        for stage in item.get("stages", []):
            stage["payload_json"] = "[redacted]"
    return item


@router.get("/quota", dependencies=[Depends(deps.require_admin)])
async def quota_list(provider: str | None = None, force: bool = False) -> dict[str, Any]:
    """Live quota per connection (cache TTL quota.ttl_s). A provider that does not
    declare a usage spec reports available=False, never zero."""
    from engrix_router.api.admin_connections import resolve_pair
    from engrix_router.accounts import connections

    targets = connections.list_for_provider(provider, include_health=False)
    results = []
    for row in targets:
        try:
            definition, creds = resolve_pair(row["id"])
        except HTTPException:
            continue
        reading = await limits.get_quota(registry.get_provider(definition.id), creds, force=force)
        results.append({"connection_id": row["id"], "name": row["name"], "provider": row["provider"],
                        **reading})
    return {"providers": results, "worst": limits.worst_remaining()}


@router.get("/quota/history", dependencies=[Depends(deps.require_admin)])
async def quota_history(provider: str, limit: int = 200) -> list[dict[str, Any]]:
    return limits.history(provider, limit=limit)


@router.patch("/budget", dependencies=[Depends(deps.require_admin)])
async def patch_budget(payload: dict[str, Any]) -> dict[str, Any]:
    scope = str(payload.get("scope") or "")
    if not scope:
        raise HTTPException(400, "requires {scope, token_limit?, req_limit?}")
    return budget.set_limits(scope=scope, token_limit=payload.get("token_limit"),
                                   req_limit=payload.get("req_limit"))


@router.get("/logs", dependencies=[Depends(deps.require_admin)])
async def logs_tail(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
    return {"lines": applog.tail(limit=limit)}


@router.delete("/logs", dependencies=[Depends(deps.require_admin)])
async def logs_clear() -> dict[str, Any]:
    applog.clear()
    return {"cleared": True}


@router.get("/logs/stream", dependencies=[Depends(deps.require_admin)])
async def logs_stream(seconds: int = Query(default=120, ge=1, le=3600)) -> StreamingResponse:
    """SSE live log. The pattern is copied from 9router (console-logs/stream): send
    the buffer first as an init frame, then push line by line, plus keepalive, and
    unsubscribe on abort -- without that the listeners pile up in the process (the
    comment states it explicitly at
    src/app/api/translator/console-logs/stream/route.js:26-32)."""

    async def generate():
        queue = applog.subscribe()
        try:
            yield "data: " + applog.as_json({"type": "init", "lines": applog.tail(200)}) + "\n\n"
            loop = asyncio.get_running_loop()
            deadline = loop.time() + seconds
            while loop.time() < deadline:
                try:
                    entry = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield "data: " + applog.as_json(entry) + "\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            applog.unsubscribe(queue)

    return StreamingResponse(generate(), media_type="text/event-stream")
