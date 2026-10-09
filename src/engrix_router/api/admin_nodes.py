"""Admin API: a node = an OpenAI/Anthropic-compatible provider with a runtime added.

This is the answer to the owner's request "I want to add a provider": no code to
write, no restart. 9router's counterpart is the providerNodes table +
/api/provider-nodes, but here the prefix is used as the provider ID (the model id
becomes `prefix/model`), just like the `qd/` alias at registry_qoder.js:4.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from engrix_router.api import deps
from engrix_router.storage.sqlite import execute, now_iso, query, query_one
from engrix_router.core import logs as applog
from engrix_router.storage import settings
from engrix_router.transport import url_guard
from engrix_router.core.ids import new_id
from engrix_router.providers import registry

router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(deps.require_admin)])

NODE_TYPES = {"openai-compatible", "anthropic-compatible", "custom-embedding"}
API_TYPES = {"chat", "responses", "embeddings"}


def _row(node_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    return dict(row) if row else None


@router.get("")
async def list_nodes() -> list[dict[str, Any]]:
    return [dict(row) for row in query("SELECT * FROM nodes ORDER BY prefix")]


@router.post("/validate")
async def validate_node(payload: dict[str, Any]) -> dict[str, Any]:
    """Preflight: URL shape, the SSRF guard, and (optional) a real GET /models."""
    from engrix_router.core import types
    from engrix_router.providers import openai as openai_provider

    base_url = str(payload.get("base_url") or "")
    api_key = str(payload.get("api_key") or "")
    node_type = str(payload.get("type") or "openai-compatible")
    try:
        clean = url_guard.assert_safe_url(base_url, kind="base_url")
    except ValueError as exc:
        return {"ok": False, "stage": "url", "error": str(exc)}
    if not api_key:
        return {"ok": False, "stage": "credentials", "error": "api_key is required"}
    if not payload.get("probe", True):
        return {"ok": True, "stage": "url", "base_url": clean}
    definition = openai_provider.build_node_definition(
        node_id="validate", name="validate", prefix="__validate__", base_url=clean,
    )
    provider = openai_provider.OpenAICompatibleProvider(definition)
    creds = types.Credentials(connection_id="validate", provider="__validate__", auth_type="apikey",
                                 name="validate", token=api_key)
    try:
        models = await provider.list_models(creds)
    except Exception as exc:
        return {"ok": False, "stage": "models", "base_url": clean,
                "error": f"{exc.__class__.__name__}: {exc}"[:400]}
    return {"ok": True, "stage": "models", "base_url": clean, "models_seen": len(models),
            "sample": [model.id for model in models[:8]]}


@router.post("", status_code=201)
async def create_node(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    prefix = str(payload.get("prefix") or "").strip().lower()
    node_type = str(payload.get("type") or "openai-compatible")
    api_type = str(payload.get("api_type") or "chat")
    base_url = str(payload.get("base_url") or "")
    if not name:
        raise HTTPException(400, "name is required")
    if not _valid_prefix(prefix):
        raise HTTPException(400, f"invalid prefix: '{prefix}' (lowercase letters/digits/-/_, 2-32 chars)")
    if node_type not in NODE_TYPES:
        raise HTTPException(400, f"type must be one of {sorted(NODE_TYPES)}")
    if api_type not in API_TYPES:
        raise HTTPException(400, f"api_type must be one of {sorted(API_TYPES)}")
    if query_one("SELECT id FROM nodes WHERE prefix = ? OR name = ?", (prefix, name)):
        raise HTTPException(409, f"a node with that prefix/name already exists (prefix collision = silent misrouting)")
    try:
        clean = url_guard.assert_safe_url(base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if settings.get_bool("nodes.validate_before_create"):
        verdict = await validate_node({"base_url": clean, "api_key": payload.get("api_key") or "",
                                       "type": node_type})
        if not verdict["ok"] and verdict.get("stage") == "models":
            raise HTTPException(422, f"upstream unreachable: {verdict.get('error')}")
    node_id = new_id()
    execute(
        "INSERT INTO nodes(id, name, prefix, type, api_type, base_url, is_active, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,1,?,?)",
        (node_id, name, prefix, node_type, api_type, clean, now_iso(), now_iso()),
    )
    registry.invalidate_nodes()
    applog.info(applog.NS_PROVIDER, f"node created: {prefix} -> {clean}")
    return _row(node_id) or {"id": node_id}


@router.patch("/{node_id}")
async def patch_node(node_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if _row(node_id) is None:
        raise HTTPException(404, "node not found")
    columns: dict[str, Any] = {}
    for key in ("name", "api_type", "type", "is_active"):
        if key in payload:
            columns[key] = payload[key]
    if "prefix" in payload:
        prefix = str(payload["prefix"]).strip().lower()
        if not _valid_prefix(prefix):
            raise HTTPException(400, f"invalid prefix: '{prefix}'")
        clash = query_one("SELECT id FROM nodes WHERE prefix = ? AND id != ?", (prefix, node_id))
        if clash:
            raise HTTPException(409, f"prefix {prefix} already used by another node")
        columns["prefix"] = prefix
    if "base_url" in payload:
        try:
            columns["base_url"] = url_guard.assert_safe_url(str(payload["base_url"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if not columns:
        raise HTTPException(400, "nothing to change")
    columns["updated_at"] = now_iso()
    sets = ", ".join(f"{key} = ?" for key in columns)
    params = list(columns.values()) + [node_id]
    execute(f"UPDATE nodes SET {sets} WHERE id = ?", params)  # noqa: S608 -- key dari whitelist lokal
    registry.invalidate_nodes()
    return _row(node_id) or {"id": node_id}


@router.delete("/{node_id}")
async def delete_node(node_id: str) -> dict[str, Any]:
    row = _row(node_id)
    if row is None:
        raise HTTPException(404, "node not found")
    bound = query_one("SELECT COUNT(*) AS n FROM connections WHERE node_id = ?", (node_id,))
    execute("DELETE FROM nodes WHERE id = ?", (node_id,))  # connections.node_id ikut CASCADE
    registry.invalidate_nodes()
    applog.warn(applog.NS_PROVIDER, f"node {row['prefix']} deleted; {int(bound['n']) if bound else 0} connections deleted with it")
    return {"deleted": node_id, "cascaded_connections": int(bound["n"]) if bound else 0}


def _valid_prefix(prefix: str) -> bool:
    return bool(prefix) and 2 <= len(prefix) <= 32 and prefix.replace("-", "").replace("_", "").isalnum()
