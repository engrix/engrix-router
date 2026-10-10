"""Admin API: runtime settings, process status, and the protocol-drift reset.

The pattern is copied from 9router: GET returns a flat object (not wrapped), and
PATCH holds a denylist of keys that may not be written through the API
(src/app/api/settings/route.js:15 PROTECTED_SETTING_KEYS -- anti mass-assignment,
which let a password get overwritten). This gateway has no such protected key
(credentials never travel through settings), yet the pattern is kept so the
endpoint cannot be used to reset keys computed as derived state.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from engrix_router.core import config
from engrix_router.api import deps
from engrix_router.storage.sqlite import table_names
from engrix_router.accounts import catalog, health
from engrix_router.storage import settings
from engrix_router.providers import registry
from engrix_router.transport import http_client

router = APIRouter(prefix="/api", tags=["settings"])

DERIVED_KEYS = frozenset({
    "health.drift_until_ms",  # state, bukan config: hanya bisa di-reset via /api/drift/clear
    "health.drift_reason",
})


@router.get("/settings", dependencies=[Depends(deps.require_admin)])
async def get_settings() -> dict[str, Any]:
    values = settings.all_settings()
    docs = {key: {"default": default, "type": typ.__name__, "doc": doc,
                  # grup = segmen pertama nama kunci (budget.*, health.*);
                  # knob provider pakai id provider-nya sendiri sebagai grup
                  # (register_defaults mewajibkan prefix owner.)
                  "group": key.split(".", 1)[0]}
            for key, (default, typ, doc) in settings.DEFAULTS.items()}
    return {"values": values, "schema": docs}


@router.patch("/settings", dependencies=[Depends(deps.require_admin)])
async def patch_settings(payload: dict[str, Any]) -> dict[str, Any]:
    if not payload:
        raise HTTPException(400, "body is empty")
    applied = {}
    for key, value in payload.items():
        if key in DERIVED_KEYS:
            raise HTTPException(409, f"{key} is derived state -- use /api/drift/clear")
        try:
            applied[key] = settings.set_setting(key, value, actor="admin-api")
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"applied": applied}


@router.get("/state", dependencies=[Depends(deps.require_admin)])
async def process_state() -> dict[str, Any]:
    """Introspection: providers, nodes, caches, HTTP pools, health, drift."""
    return {
        "process": config.summary(),
        "providers": [{"id": d.id, "prefixes": list(d.prefixes), "base_url": d.transport.base_url,
                       "is_node": "node" in d.features} for d in registry.definitions()],
        "catalog_cache": catalog.stats(),
        "http_pools": http_client.open_connections(),
        "health": health.snapshot(),
        "drift": health.drift_state(),
        "tables": table_names(),
    }


@router.post("/drift/clear", dependencies=[Depends(deps.require_admin)])
async def clear_drift() -> dict[str, Any]:
    health.clear_drift()
    return {"drift": health.drift_state()}


@router.post("/cache/invalidate", dependencies=[Depends(deps.require_admin)])
async def invalidate_cache(provider: str | None = None, connection_id: str | None = None) -> dict[str, Any]:
    removed = catalog.invalidate(provider, connection_id)
    return {"catalog_entries_removed": removed}
