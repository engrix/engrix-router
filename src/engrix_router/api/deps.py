"""FastAPI dependencies: client authentication vs admin authentication, plus error response helpers.

Two separate gates (9router also has two, but their sources are scattered: the
dashboardGuard.js middleware plus a per-handler check at
src/sse/handlers/chat.js:72 -- leaks easily, because forgetting one check is
enough). Here:
  * /v1*   -> client api key (api_keys table), can be switched off through settings
              when you only want localhost access. Loopback does NOT automatically
              pass: 9router lets every loopback request through
              (dashboardGuard.js:123-135), which means any process on the same
              machine can call a gateway that drains quota.
  * /api/* -> EROUTER_ADMIN_TOKEN. Empty env => the admin API is REFUSED entirely
              (fail-closed), never opened up instead.
"""
from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException, Request, status

from engrix_router.core import config
from engrix_router.identity import api_keys
from engrix_router.core import errors


def _denied(error_class: str, message: str, *, http_status: int, retry_after_s: int | None = None):
    from fastapi.responses import JSONResponse

    payload = errors.client_error_body(error_class, message, retry_after_s=retry_after_s)
    return JSONResponse(payload, status_code=http_status,
                        headers={"Retry-After": str(retry_after_s)} if retry_after_s else None)


async def require_client_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_goog_api_key: str | None = Header(default=None),
) -> dict[str, Any] | None:
    """Return the client's {id,name}, or None when the gate is switched off via env.

    The single source of this gate is config.REQUIRE_CLIENT_KEY (env) -- deliberately
    NOT in the settings table, so that two places cannot configure the same thing.
    """
    if not config.REQUIRE_CLIENT_KEY:
        return None
    secret = api_keys.extract_client_key(
        authorization=authorization, x_api_key=x_api_key, x_goog_api_key=x_goog_api_key,
        query_key=request.query_params.get("key"),
    )
    if not secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=errors.client_error_body(errors.CLASS_CLIENT_NO_KEY,
                                                            "Gateway API key was not sent"))
    record = api_keys.verify(secret)
    if record is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=errors.client_error_body(errors.CLASS_CLIENT_BAD_KEY,
                                                            "Gateway API key is unknown or has been revoked"))
    return record


async def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    if not config.ADMIN_IS_CONFIGURED:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=errors.client_error_body(
                errors.CLASS_UPSTREAM_UNAVAILABLE,
                "Admin API locked: EROUTER_ADMIN_TOKEN is not set (fail-closed on purpose, "
                "no hardcoded default)",
            ),
        )
    sent = ""
    if authorization and authorization.lower().startswith("bearer "):
        sent = authorization[7:].strip()
    elif x_admin_token:
        sent = x_admin_token.strip()
    elif request.query_params.get("admin_token"):
        sent = request.query_params["admin_token"]
    if not sent or sent != config.ADMIN_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail=errors.client_error_body(errors.CLASS_CLIENT_BAD_KEY,
                                                            "admin token is incorrect"))


def detail_of(exc: HTTPException) -> dict[str, Any]:
    detail = exc.detail
    return detail if isinstance(detail, dict) else {"error": {"message": str(detail)}}
