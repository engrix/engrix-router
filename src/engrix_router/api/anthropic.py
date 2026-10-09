"""Inbound Anthropic Messages API: `POST /v1/messages`.

This route is the acceptance test for the canonical format (ADR-0001): nothing in the
pipeline knows Anthropic exists. The body is translated to canonical, the same
admission / routing / retry / trace path runs, and the frames are translated back.

Differences from `/v1/chat/completions` that are the client's expectation, not ours:
  * `model` usually arrives WITHOUT a provider prefix (`claude-...`). A bare id gets the
    `anthropic/` prefix here; a prefixed id is respected. `/v1/*` refuses a bare model
    on purpose, because that client (engrix) always writes the prefix and guessing a
    vendor from an empty string is how traffic ends up at the wrong upstream.
  * auth also accepts `x-api-key`, which `api/deps.py` already extracts for every route.
  * errors use the Anthropic error shape and the stream ends with `event: error`
    instead of a `data:` frame plus [DONE].
"""
from __future__ import annotations

from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from engrix_router.core import config
from engrix_router.core.formats import anthropic as anthropic_fmt
from engrix_router.core import errors
from engrix_router.core.ids import new_id
from engrix_router.core import logs as applog
from engrix_router.api import deps
from engrix_router.pipeline import runner

router = APIRouter()

DEFAULT_PROVIDER = "anthropic"


def _canonical(body: dict[str, Any]) -> dict[str, Any]:
    canonical = anthropic_fmt.to_canonical_body(body)
    model = str(canonical.get("model") or "")
    if model and "/" not in model:
        canonical["model"] = f"{DEFAULT_PROVIDER}/{model}"
    return canonical


def _error_response(exc: runner.RequestRejected) -> JSONResponse:
    classified = exc.classified
    kind = "invalid_request_error" if classified.client_status < 500 else "api_error"
    status = classified.client_status
    headers: dict[str, str] = {}
    retry_after = classified.retry_after_effective(0)
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    payload = anthropic_fmt.error_body(exc.message, kind=kind, status=status)
    payload["error"]["code"] = classified.error_class
    if classified.retry_after_s:
        payload["error"]["retry_after"] = classified.retry_after_s
    return JSONResponse(payload, status_code=status, headers=headers)


@router.post("/v1/messages")
async def messages(request: Request,
                   client: dict | None = Depends(deps.require_client_key)):
    raw = await request.body()
    if len(raw) > config.MAX_REQUEST_BYTES:
        return JSONResponse(anthropic_fmt.error_body(
            f"body of {len(raw)} bytes exceeds the limit of {config.MAX_REQUEST_BYTES}",
            kind="invalid_request_error", status=413), status_code=413)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(anthropic_fmt.error_body("body is not valid JSON",
                                                 kind="invalid_request_error", status=400),
                            status_code=400)
    if not isinstance(body, dict):
        return JSONResponse(anthropic_fmt.error_body("body must be a JSON object",
                                                 kind="invalid_request_error", status=400),
                            status_code=400)
    canonical = _canonical(body)
    stream = bool(body.get("stream", False))
    lane = str(body.get("_lane") or "interactive")
    api_key_id = client["id"] if client else None
    model = str(canonical.get("model") or "")
    try:
        if stream:
            return StreamingResponse(
                _stream(canonical, api_key_id=api_key_id, lane=lane),
                media_type="text/event-stream",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no",
                         "x-engrix-request": "stream"},
            )
        completion = await runner.run_chat(canonical, api_key_id=api_key_id, lane=lane,
                                           endpoint="/v1/messages")
        return JSONResponse(anthropic_fmt.from_canonical_completion(
            str(completion.get("id") or "").removeprefix("chatcmpl-") or "unknown",
            model, completion))
    except runner.RequestRejected as exc:
        return _error_response(exc)
    except Exception as exc:  # jangan bocor stack trace ke klien
        applog.error(applog.NS_ROUTE, f"gateway bug: {exc.__class__.__name__}: {exc}")
        classified = errors.classify(text=f"{exc.__class__.__name__}: {exc}",
                                     exception_name=exc.__class__.__name__)
        return JSONResponse(anthropic_fmt.error_body("internal gateway error",
                                                 status=classified.client_status),
                            status_code=classified.client_status)


async def _stream(canonical: dict[str, Any], *, api_key_id: str | None, lane: str) -> AsyncIterator[str]:
    """Render the canonical frame stream as Anthropic events (no [DONE] sentinel)."""
    request_id = new_id()
    renderer = anthropic_fmt.StreamRenderer(request_id, str(canonical.get("model") or ""))
    pending: list[str] = []

    def render(frame: dict[str, Any]) -> str:
        # Dipanggil runner SEBELUM frame di-yield, jadi baris event-nya udah siap
        # pas consumer gilirannya narik -- dan trace nyimpen byte yang beneran dikirim.
        pending[:] = renderer.push(frame)
        return "".join(pending)

    started = False
    async for frame in runner.stream_frames(canonical, api_key_id=api_key_id, lane=lane,
                                            endpoint="/v1/messages", render=render,
                                            request_id=request_id):
        if frame.get("__error__"):
            payload = anthropic_fmt.error_body(str(frame.get("message") or "upstream error"),
                                           status=int(frame.get("status") or 502))
            payload["error"]["code"] = str(frame.get("code") or errors.CLASS_UNKNOWN)
            yield anthropic_fmt.sse_event("error", payload)
            return
        if not started:
            # message_start harus jadi event pertama, dan request id-nya dibikin di
            # sini (bukan oleh runner) -- jadi gak ada yang perlu ditunggu.
            yield renderer.start()
            started = True
        for line in pending:
            yield line
    if not started:
        yield renderer.start()
    for line in renderer.finish():
        yield line
