"""The /v1* data path -- the contract that engrix-agent reads.

Body-shape rules this gateway holds to (and tests/test_compat_contract.py checks):
  * the body is ACCEPTED as sent: a field this gateway does not know is neither
    rejected nor silently dropped on its way upstream. engrix sends
    `reasoning_effort`, `verbosity`, the already merged keys from `extra_body`,
    `tool_choice`, `temperature` (engrix-agent/agent/streaming.py:417-428) and it
    expects the provider to decide what to use.
  * does `stream` default to True when the field is missing? No. This gateway
    follows the OpenAI convention: default false. engrix always sends stream=True
    explicitly.
  * engrix never sends `stream_options` on the qoder/cbai path (streaming.py:952
    sends it only on the qwen path), so the gateway must not require usage to be
    present in the stream -- that is why an estimate plus a usage trailer exist in
    pipeline/runner.py.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from engrix_router.core import config
from engrix_router.api import deps
from engrix_router.accounts import catalog, connections
from engrix_router.core import logs as applog
from engrix_router.pipeline import runner
from engrix_router.storage import settings
from engrix_router.core import errors

router = APIRouter()


def _error_response(exc: runner.RequestRejected) -> JSONResponse:
    classified = exc.classified
    body = {
        "error": {
            "message": exc.message[:900],
            "type": "server_error" if classified.client_status >= 500 else "invalid_request_error",
            "code": classified.error_class,
            "status": classified.client_status,
            "vendor_code": classified.vendor_code,
        }
    }
    retry_after = classified.retry_after_effective(settings.get_int("client.retry_after_default_s"))
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return JSONResponse(body, status_code=classified.client_status, headers=headers)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request,
                           client: dict | None = Depends(deps.require_client_key)):
    raw = await request.body()
    if len(raw) > config.MAX_REQUEST_BYTES:
        return JSONResponse(
            errors.client_error_body(errors.CLASS_CLIENT_BAD_REQUEST,
                                     f"body of {len(raw)} bytes exceeds the limit of {config.MAX_REQUEST_BYTES}"),
            status_code=413,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(errors.client_error_body(errors.CLASS_CLIENT_BAD_REQUEST,
                                                     "body is not valid JSON"), status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(errors.client_error_body(errors.CLASS_CLIENT_BAD_REQUEST,
                                                     "body must be a JSON object"), status_code=400)
    stream = bool(body.get("stream", False))
    lane = str(body.get("_lane") or "interactive")
    api_key_id = client["id"] if client else None
    try:
        if stream:
            return StreamingResponse(
                runner.stream_chat(body, api_key_id=api_key_id, lane=lane),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no",
                         "x-engrix-request": "stream"},
            )
        result = await runner.run_chat(body, api_key_id=api_key_id, lane=lane)
        return JSONResponse(result)
    except runner.RequestRejected as exc:
        return _error_response(exc)
    except Exception as exc:  # jangan bocor stack trace ke klien
        applog.error(applog.NS_ROUTE, f"gateway bug: {exc.__class__.__name__}: {exc}")
        classified = errors.classify(text=f"{exc.__class__.__name__}: {exc}", exception_name=exc.__class__.__name__)
        return JSONResponse(errors.client_error_body(errors.CLASS_UNKNOWN,
                                                     "internal gateway error"),
                            status_code=classified.client_status)


@router.get("/v1/models")
async def list_models(request: Request, client: dict | None = Depends(deps.require_client_key)):
    """Model list = the union of upstream catalogs over healthy connections, prefixed
    `provider/model` so a returned `model` value can be handed straight back by the
    client (engrix contract: the id is opaque and passed through as sent).

    Source: the TTL catalog cache (accounts/catalog.py) + the built-in list.
    A connection that is currently locked does not have its models removed, it is
    only marked.
    """
    from engrix_router.providers import registry

    entries: dict[str, dict[str, Any]] = {}
    for definition in registry.definitions():
        rows = connections.list_for_provider(definition.id, include_health=True)
        live_models: list[Any] = []
        for row in rows:
            creds = connections.load_credentials(row["id"])
            if creds is None or (not creds.token and creds.auth_type != "none"):
                continue
            try:
                live_models = await catalog.models_for(registry.get_provider(definition.id), creds)
                break
            except Exception as exc:  # upstream mati -> pakai daftar built-in
                applog.warn(applog.NS_PROVIDER, f"catalog {definition.id} failed: {exc.__class__.__name__}")
        models = live_models or list(definition.models)
        for model in models:
            public_id = f"{definition.prefixes[0]}/{model.id}"
            entries[public_id] = {
                "id": public_id,
                "object": "model",
                "created": 0,
                "owned_by": definition.id,
                "name": model.name or model.id,
                "context_length": model.context_length,
                "supports_vision": model.supports_vision,
                "supports_tools": model.supports_tools,
                "connections": len(rows),
            }
    return {"object": "list", "data": [entries[key] for key in sorted(entries)]}
