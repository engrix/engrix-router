"""Admin API: gateway client keys (`/api/keys`).

The secret value leaves the process only once, at POST time. GET never returns a
full key -- unlike 9router, whose rowToKey hands back the intact `key`
(src/lib/db/repos/apiKeysRepo.js:4-14), so anyone who can open the dashboard can
read every production key.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from engrix_router.api import deps
from engrix_router.identity import api_keys

router = APIRouter(prefix="/api/keys", tags=["keys"], dependencies=[Depends(deps.require_admin)])


@router.get("")
async def list_keys() -> list[dict[str, Any]]:
    return api_keys.list_keys()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_key(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        created = api_keys.create_key(str(payload.get("name") or ""),
                                       machine_id=payload.get("machine_id"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **created,
        "warning": "the key value is shown only now. save it.",
    }


@router.get("/{key_id}")
async def get_key(key_id: str) -> dict[str, Any]:
    for item in api_keys.list_keys():
        if item["id"] == key_id:
            return item
    raise HTTPException(status_code=404, detail="key not found")


@router.patch("/{key_id}")
async def patch_key(key_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if "is_active" in payload:
        row = api_keys.set_active(key_id, bool(payload["is_active"]))
        if row is None:
            raise HTTPException(status_code=404, detail="key not found")
    if "name" in payload:
        row = api_keys.rename(key_id, str(payload["name"]))
        if row is None:
            raise HTTPException(status_code=404, detail="key not found")
    if not payload:
        raise HTTPException(status_code=400, detail="nothing to change")
    return {"id": key_id, **(row or {})}


@router.delete("/{key_id}", status_code=status.HTTP_200_OK)
async def delete_key(key_id: str) -> Response:
    if not api_keys.delete_key(key_id):
        raise HTTPException(status_code=404, detail="key not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
