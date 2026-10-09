"""Admin API: proxy pools (`/api/proxy-pools`).

Proxy credentials never travel through the API or the DB -- env only
(credential_hint). In 9router the complete URL including user:pass was stored on
the pool row, and those credentials once leaked in full into a plaintext report.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from engrix_router.api import deps
from engrix_router.transport import proxy

router = APIRouter(prefix="/api/proxy-pools", tags=["proxy"],
                   dependencies=[Depends(deps.require_admin)])


@router.get("")
async def list_pools(include_usage: bool = False) -> list[dict[str, Any]]:
    return proxy.list_pools(include_usage=include_usage)


@router.post("", status_code=201)
async def create_pool(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return proxy.create(
            name=str(payload.get("name") or ""),
            proxy_url=str(payload.get("proxy_url") or ""),
            no_proxy=payload.get("no_proxy"),
            type_=str(payload.get("type") or "http"),
            strict_proxy=bool(payload.get("strict_proxy", False)),
            credential_hint=payload.get("credential_hint"),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/{pool_id}")
async def get_pool(pool_id: str) -> dict[str, Any]:
    row = proxy.get(pool_id)
    if row is None:
        raise HTTPException(404, "pool not found")
    return row


@router.patch("/{pool_id}")
async def patch_pool(pool_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if proxy.get(pool_id) is None:
        raise HTTPException(404, "pool not found")
    try:
        row = proxy.update(pool_id, payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if row is None:
        raise HTTPException(404, "pool not found")
    return row


@router.delete("/{pool_id}")
async def delete_pool(pool_id: str) -> dict[str, Any]:
    if not proxy.delete(pool_id):
        raise HTTPException(404, "pool not found")
    return {"deleted": pool_id}


@router.post("/{pool_id}/test")
async def test_pool(pool_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    probe_url = (payload or {}).get("probe_url")
    try:
        return await proxy.test(pool_id, probe_url=probe_url)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"[:300]}
