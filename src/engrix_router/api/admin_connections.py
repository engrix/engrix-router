"""Admin API: upstream connections (credentials + priority + health + debug).

Routes are split away from /api/providers: in 9router the single path
`/api/providers/[id]` serves BOTH things (the built-in provider list and the
connection instance CRUD), so the dashboard has to guess which is which. Here:
  /api/providers            = list of provider definitions (built-in + nodes)
  /api/connections          = credential instances belonging to a provider
  /api/connections/{id}/... = actions on one instance (test, models, test-models)
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from engrix_router.api import deps
from engrix_router.accounts import catalog, connections, health
from engrix_router.pipeline import runner
from engrix_router.core.types import Credentials
from engrix_router.providers import registry

router = APIRouter(prefix="/api", tags=["providers"])


@router.get("/providers", dependencies=[Depends(deps.require_admin)])
async def list_providers() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for definition in registry.definitions():
        rows = connections.list_for_provider(definition.id, include_health=False)
        out.append({
            "id": definition.id,
            "display_name": definition.display_name or definition.id,
            "category": definition.category,
            "prefixes": list(definition.prefixes),
            "base_url": definition.transport.base_url,
            "auth_modes": list(definition.auth_modes),
            "probe_tier": definition.probe_tier,
            "features": sorted(definition.features),
            "has_usage": definition.transport.usage is not None,
            "models_declared": [model.id for model in definition.models],
            "connections": len(rows),
            "active_connections": sum(1 for row in rows if row["is_active"]),
            "is_node": "node" in definition.features,
        })
    return out


@router.get("/connections", dependencies=[Depends(deps.require_admin)])
async def list_connections(provider: str | None = Query(default=None)) -> list[dict[str, Any]]:
    return connections.list_for_provider(provider)


@router.post("/connections", dependencies=[Depends(deps.require_admin)], status_code=201)
async def create_connection(payload: dict[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider") or "").strip()
    definition = registry.get_definition(provider)
    if definition is None:
        raise HTTPException(400, f"provider '{provider}' is unknown; create a node first via /api/nodes")
    try:
        row = connections.create(
            provider=provider,
            auth_type=str(payload.get("auth_type") or "apikey"),
            name=payload.get("name"),
            email=payload.get("email"),
            api_key=payload.get("api_key"),
            access_token=payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
            expires_at=payload.get("expires_at"),
            priority=payload.get("priority"),
            provider_specific=payload.get("provider_specific") or {},
            proxy_pool_id=payload.get("proxy_pool_id"),
            is_active=bool(payload.get("is_active", True)),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    # Sekaligus probe (0 kredit): koneksi baru langsung punya katalog model, jadi
    # chat pertama gak gagal gara-gara model_config belum disalin dari vendor.
    # Gagal probe bukan gagal create -- hasilnya cuma ditempel di respons.
    if payload.get("probe", True):
        creds = connections.load_credentials(row["id"])
        if creds is not None:
            result = await registry.get_provider(provider).probe(creds, definition.transport.default_model)
            row["probe"] = result.as_dict()
            health.mark_probe(row["id"], result.as_dict())
    return row


@router.get("/connections/{connection_id}", dependencies=[Depends(deps.require_admin)])
async def get_connection(connection_id: str) -> dict[str, Any]:
    row = connections.get_for_dashboard(connection_id)
    if row is None:
        raise HTTPException(404, "connection not found")
    row["usage_7d"] = connections.usage_summary(connection_id)
    row["quota"] = None
    return row


@router.patch("/connections/{connection_id}", dependencies=[Depends(deps.require_admin)])
async def patch_connection(connection_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    row = connections.update(connection_id, payload)
    if row is None:
        raise HTTPException(404, "connection not found")
    return row


@router.delete("/connections/{connection_id}", dependencies=[Depends(deps.require_admin)])
async def delete_connection(connection_id: str) -> dict[str, Any]:
    if not connections.delete(connection_id):
        raise HTTPException(404, "connection not found")
    catalog.invalidate()
    return {"deleted": connection_id}


@router.post("/connections/reorder", dependencies=[Depends(deps.require_admin)])
async def reorder(payload: dict[str, Any]) -> dict[str, Any]:
    provider = str(payload.get("provider") or "")
    ordered = payload.get("ids") or []
    if not provider or not isinstance(ordered, list):
        raise HTTPException(400, "requires {provider, ids:[...]}")
    return {"provider": provider, "reordered": connections.reorder(provider, [str(i) for i in ordered])}


@router.get("/connections/{connection_id}/models", dependencies=[Depends(deps.require_admin)])
async def connection_models(connection_id: str, force: bool = Query(default=False)) -> dict[str, Any]:
    definition, creds = resolve_pair(connection_id)
    try:
        models = await catalog.models_for(registry.get_provider(definition.id), creds, force=force)
    except Exception as exc:
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:400], "models": []}
    return {"ok": True, "cached": not force, "provider": definition.id,
            "models": catalog.as_public_dicts(models)}


@router.post("/connections/{connection_id}/test", dependencies=[Depends(deps.require_admin)])
async def test_connection(connection_id: str) -> dict[str, Any]:
    """Cheap probe (models_list tier). Counterpart of 9router's
    POST /api/providers/[id]/test, but with no giant switch hardcoded per provider
    (testUtils.js:540-866) -- every provider must implement probe(), so a new
    provider is debuggable immediately."""
    definition, creds = resolve_pair(connection_id)
    provider = registry.get_provider(definition.id)
    result = await provider.probe(creds, definition.transport.default_model)
    state = health.mark_probe(connection_id, result.as_dict())
    return {**result.as_dict(), "provider": definition.id, "connection_id": connection_id,
            "test_status": state["test_status"]}


@router.post("/connections/{connection_id}/test_models", dependencies=[Depends(deps.require_admin)])
async def test_models(connection_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ping every model through our own /v1/chat/completions -- the debug path and
    the traffic path must run the same code (the pattern of 9router's
    /api/providers/[id]/test-models, which loops back to its own gateway).

    THIS SPENDS QUOTA. It requires an explicit {"allow_spend": true}.
    """
    body = payload or {}
    if not body.get("allow_spend"):
        raise HTTPException(412, "test-models calls the real upstream; send allow_spend=true")
    definition, creds = resolve_pair(connection_id)
    models = await catalog.models_for(registry.get_provider(definition.id), creds)
    if not models:
        return {"provider": definition.id, "results": [], "note": "no models to test"}
    results: list[dict[str, Any]] = []
    limit = max(1, min(int(body.get("max_models") or 1), len(models)))
    for model in models[:limit]:
        chat_body = {
            "model": f"{definition.prefixes[0]}/{model.id}",
            "messages": [{"role": "user", "content": "reply with OK"}],
            "max_tokens": 8,
            "stream": False,
        }
        started = time.perf_counter()
        try:
            await runner.run_chat(chat_body, lane="debug")
            results.append({"model": model.id, "ok": True,
                            "latency_ms": int((time.perf_counter() - started) * 1000)})
        except runner.RequestRejected as exc:
            results.append({"model": model.id, "ok": False, "status": exc.classified.client_status,
                            "error_class": exc.classified.error_class, "error": exc.message[:300]})
        except Exception as exc:  # pragma: no cover
            results.append({"model": model.id, "ok": False,
                            "error": f"{exc.__class__.__name__}: {exc}"[:300]})
    return {"provider": definition.id, "connection_id": connection_id, "results": results}


def resolve_pair(connection_id: str) -> tuple[Any, Credentials]:
    row = connections.get_for_dashboard(connection_id)
    if row is None:
        raise HTTPException(404, "connection not found")
    definition = registry.get_definition(row["provider"])
    if definition is None:
        raise HTTPException(409, f"provider {row['provider']} is not registered (node deleted?)")
    creds = connections.load_credentials(connection_id)
    if creds is None:
        raise HTTPException(409, "credentials could not be loaded")
    return definition, creds
