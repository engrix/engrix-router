"""Admin API: browser-based account linking for oauth providers (the "OAuth+" flow).

9router asks the operator to paste tokens by hand. Here the gateway runs the
vendor's own login flow instead: the provider package contributes two hooks --
``oauth_start`` (returns the vendor's authorize URL + an opaque flow token) and
``oauth_poll`` (checks the flow; on approval returns the finished account).
Core owns only the state machine and the persistence: sessions live in memory
(TTL-bounded, single-use) and the result lands in one ``connections`` row,
ready for the health/quota machinery. Vendor knowledge (URLs, client ids,
token shapes) never appears in this file -- it belongs to the private package
(ADR-0002), so nothing here tells any client it talks to a specific vendor.

Why polling instead of a redirect callback: the gateway is a local server that
may sit behind NAT without a public URL, exactly like 9router's CLI mode; the
vendor's server-side code relay (init -> authorize in a browser -> poll) makes
a loopback listener unnecessary, and the whole flow survives headless setups.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from engrix_router.accounts import connections
from engrix_router.api import deps
from engrix_router.providers import registry

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

# # Registry sesi sengaja in-process: link itu gestur interaktif pendek, bukan
# # data permanen. Kalau restart buatin sesi ilang, operator tinggal klik OAuth+
# # lagi — gak ada state setengah jadi yang nyangkut di disk.
_SESSIONS: dict[str, dict[str, Any]] = {}
_SESSION_TTL_MS = 10 * 60 * 1000
_MAX_SESSIONS = 64
_MIN_POLL_INTERVAL_S = 2


def _now_ms() -> int:
    return int(time.time() * 1000)


def _prune() -> None:
    now = _now_ms()
    for key in [k for k, v in _SESSIONS.items()
                if v["expires_at_ms"] <= now] + list(_SESSIONS)[_MAX_SESSIONS:]:
        _SESSIONS.pop(key, None)


def _require_hook(provider: Any, name: str) -> Any:
    hook = getattr(provider, name, None)
    if hook is None:
        raise HTTPException(409, f"provider '{provider.id}' does not support OAuth linking")
    return hook


@router.post("/{provider_id}/start", dependencies=[Depends(deps.require_admin)])
async def start_link(provider_id: str) -> dict[str, Any]:
    definition = registry.get_definition(provider_id)
    if definition is None:
        raise HTTPException(404, f"unknown provider '{provider_id}'")
    # # Kategori definisi = satu-satunya gerbang: provider apikey nggak punya
    # # hak menjalankan alur browser-link meskipun modulnya diam-diam memasang hook.
    if definition.category != "oauth":
        raise HTTPException(409, f"provider '{provider_id}' is not an OAuth provider")
    provider = registry.get_provider(definition.id)
    hook = _require_hook(provider, "oauth_start")
    try:
        flow = await hook()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"vendor flow start failed: {type(exc).__name__}: {exc}"[:300])
    if not isinstance(flow, dict) or not flow.get("authorize_url") or not flow.get("flow_token"):
        raise HTTPException(502, "provider returned an incomplete OAuth start payload")
    _prune()
    session_id = uuid.uuid4().hex
    expires_at_ms = _now_ms() + _SESSION_TTL_MS
    _SESSIONS[session_id] = {
        "provider": definition.id,
        "flow_token": str(flow["flow_token"]),
        "expires_at_ms": expires_at_ms,
        "poll_after_ms": 0,
    }
    interval = flow.get("poll_interval_sec") or _MIN_POLL_INTERVAL_S
    return {
        "session_id": session_id,
        "provider": definition.id,
        # # Link buat operator: apa pun yang dikasih vendor dipakai apa adanya --
        # # core tidak tahu dan tidak boleh tahu bentuk URL vendor.
        "authorize_url": str(flow["authorize_url"]),
        "expires_at": expires_at_ms,
        "poll_interval_sec": max(_MIN_POLL_INTERVAL_S, int(interval)),
    }


@router.get("/{provider_id}/{session_id}", dependencies=[Depends(deps.require_admin)])
async def poll_link(provider_id: str, session_id: str) -> dict[str, Any]:
    session = _SESSIONS.get(session_id)
    if session is None or session["provider"] != provider_id:
        raise HTTPException(404, "link session not found (expired or already used)")
    if session["expires_at_ms"] <= _now_ms():
        _SESSIONS.pop(session_id, None)
        return {"status": "expired"}
    if session["poll_after_ms"] > _now_ms():
        # # Rate-limit lokal: browser yang polling agresif tidak diteruske
        # # ke vendor -- pola 9router interval di-hammer, kita hormati interval.
        return {"status": "pending", "retry_after_ms": session["poll_after_ms"] - _now_ms()}
    provider = registry.get_provider(provider_id)
    hook = _require_hook(provider, "oauth_poll")
    try:
        result = await hook(session["flow_token"])
    except Exception as exc:
        _SESSIONS.pop(session_id, None)
        return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:280]}
    status = (result or {}).get("status")
    if status == "pending":
        interval_s = (result or {}).get("poll_interval_sec") or _MIN_POLL_INTERVAL_S
        session["poll_after_ms"] = _now_ms() + max(_MIN_POLL_INTERVAL_S, int(interval_s)) * 1000
        return {"status": "pending"}
    if status != "ready":
        _SESSIONS.pop(session_id, None)
        return {"status": "failed", "error": str((result or {}).get("error") or "provider refused")}
    account = (result or {}).get("account") or {}
    access_token = str(account.get("access_token") or "")
    if not access_token:
        # # connections.create menolak oauth tanpa access_token; gagal di sini
        # # lebih jujur daripada baris setengah jadi masuk DB.
        _SESSIONS.pop(session_id, None)
        return {"status": "failed", "error": "provider returned a ready account without an access token"}
    _SESSIONS.pop(session_id, None)  # single-use: satu link = satu koneksi
    row = connections.create(
        provider=provider_id,
        auth_type="oauth",
        name=str(account.get("name") or account.get("email") or f"{provider_id} account"),
        email=account.get("email") or None,
        access_token=access_token,
        refresh_token=account.get("refresh_token") or None,
        api_key=account.get("api_key") or None,
        provider_specific=account.get("provider_specific") or None,
    )
    return {
        "status": "ready",
        "connection_id": row["id"],
        "provider": provider_id,
        "name": row.get("name"),
    }
