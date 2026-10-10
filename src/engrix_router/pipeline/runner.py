"""
Orchestrate one request: resolve -> admission -> pick connection -> send ->
retry/failover -> record.

Four design decisions differ from 9router, each with a concrete reason:

1. The retry ladder lives here, not in the provider.
   9router keeps it in executors/base.js:100-186, but QoderExecutor overrides
   execute() wholesale (executors/qoder.js:613) so that ladder never ran for
   qoder: 0 retries, 0 URL fallback, unnoticed for versions. A provider here
   owns exactly one attempt.
2. Failover only before the first byte reaches the client.
   Once HTTP 200 is on the wire the status cannot change; later failures are
   returned as an error frame + [DONE] with no fake finish_reason - same shape
   and reasoning as open-sse/utils/streamHelpers.js:129-147.
3. Our deadlines are tighter than 9router's, and that is functional, not aesthetic.
   9router has no first-token timeout for qoder (STREAM_FIRST_CHUNK_TIMEOUT_MS
   200 s only applies to kiro, executors/kiro.js:373) while engrix abandons a
   round at 30 s (engrix-agent/agent/streaming.py:62) and retries => double
   upstream work. The gateway cuts off at 25 s
   (config.UPSTREAM_FIRST_CHUNK_TIMEOUT_S).
4. The failover loop has an attempt cap and a global deadline.
   9router's loop is `while(true)` over the connection list with neither
   (src/sse/handlers/chat.js:235-343).
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from engrix_router.core import config
from engrix_router.storage.sqlite import now_ms
from engrix_router.core import logs as applog
from engrix_router.accounts import health
from engrix_router.hooks import budget, ratelimit
from engrix_router.routing import selector
from engrix_router.storage import settings
from engrix_router.core import errors
from engrix_router.core.formats import openai as openai_fmt
from engrix_router.subscribers import trace as trace_mod, usage as usage_mod
from engrix_router.core.types import ProviderDef, UpstreamError
from engrix_router.providers import registry


class RequestRejected(Exception):
    """
    Rejected before sending, or out of options after sending.

    `classified` is always a Classified (never a dict) so the caller just
    reads .client_status / .error_class.

    """

    def __init__(self, classified: errors.Classified, message: str | None = None) -> None:
        text = message or classified.message
        super().__init__(text)
        self.classified = classified
        self.message = text


def estimate_prompt_tokens(body: dict[str, Any]) -> int:
    return usage_mod.estimate_from_body(body)["prompt"]


def _resolve(body: dict[str, Any]) -> tuple[ProviderDef, str]:
    raw = body.get("model")
    if not isinstance(raw, str) or not raw.strip():
        message = "field 'model' must be a string shaped provider/model"
        raise RequestRejected(errors.classify(status=400, text=message))
    try:
        definition, model = registry.resolve(raw)
    except (registry.UnknownModel, registry.UnknownProvider) as exc:
        raise RequestRejected(errors.classify(status=404, text=str(exc)),
                              f"model cannot be routed: {exc}") from exc
    return definition, model


async def _admission(*, provider: str, api_key_id: str | None, body: dict[str, Any], lane: str) -> int:
    drift = health.drift_state()
    if drift["tripped"]:
        raise RequestRejected(
            errors.classify(status=503, text="gateway is frozen"),
            f"upstream frozen until {drift['until_iso']} (reason: {drift['reason']})",
        )
    est = estimate_prompt_tokens(body)
    limiter = ratelimit.check_admission(api_key_id=api_key_id, provider=provider)
    if not limiter["allowed"]:
        blocked = limiter["blocked_by"][0]
        message = (f"rate limit reached scope={blocked['scope']} "
                   f"limit={blocked['limit']} per {blocked['window_s']}s")
        raise RequestRejected(errors.for_class(errors.CLASS_RATE_LIMIT, message,
                                               retry_after_s=ratelimit.retry_after(time.monotonic())),
                              message)
    decision = budget.check_admission(provider=provider, api_key_id=api_key_id, est_tokens=est, lane=lane)
    if not decision["allowed"]:
        blocked = decision["blocked_by"][0]
        message = (f"daily budget reached scope={blocked['scope']} "
                   f"used={blocked['used']}/{blocked['limit']} wanted={blocked['wanted']}")
        raise RequestRejected(errors.for_class(errors.CLASS_BUDGET_EXCEEDED, message,
                                               retry_after_s=3600), message)
    return est


def _sleep_before_retry(classified: errors.Classified, definition: ProviderDef) -> float:
    """
    Short pause before moving to another connection.

    We do NOT sleep for the vendor's Retry-After (that can be 15 minutes) -
    that belongs to health/lock state, not to blocking the event loop.

    """
    if classified.retry_after_s is not None:
        return min(float(classified.retry_after_s), 1.0)
    spec = definition.transport.retry.get(classified.client_status)
    return min(spec.delay_ms, 1000) / 1000.0 if spec else 0.0


def _chunk_for_client(frame: dict[str, Any], *, request_id: str, model: str, created: int) -> dict[str, Any]:
    out = dict(frame)
    out["id"] = f"chatcmpl-{request_id}"
    out["model"] = model
    out.setdefault("created", created)
    out.setdefault("object", "chat.completion.chunk")
    return out


def _delta_content_len(frame: dict[str, Any]) -> int:
    total = 0
    for choice in frame.get("choices") or []:
        total += len(str((choice.get("delta") or {}).get("content") or ""))
    return total


def _delta_reasoning_len(frame: dict[str, Any]) -> int:
    # # Delta reasoning (thinking) yang udah keluar ke klien. Bukan konten,
    # # cuma dipakai buat estimate token reasoning yg gak dilaporkan vendor.
    total = 0
    for choice in frame.get("choices") or []:
        total += len(str((choice.get("delta") or {}).get("reasoning_content") or ""))
    return total


def _completion_as_chunk(response: dict[str, Any], *, model: str) -> dict[str, Any]:
    """
    Wrap a chat.completion into a single chunk.

    So the non-stream and stream paths share one recording pipeline instead
    of two.

    """
    choices = response.get("choices") or []
    first = choices[0] if choices else {}
    message = first.get("message") or {}
    delta: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
    for key in ("reasoning_content", "reasoning"):
        if message.get(key):
            delta["reasoning_content"] = message[key]
            break
    if message.get("tool_calls"):
        delta["tool_calls"] = message["tool_calls"]
    return {
        "id": response.get("id") or "",
        "object": "chat.completion.chunk",
        "created": response.get("created") or int(time.time()),
        "model": response.get("model") or model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": openai_fmt.normalize_finish_reason(first.get("finish_reason") or "stop"),
        }],
        "usage": response.get("usage") or {},
    }


async def _dispatch(
    *, payload: dict[str, Any], definition: ProviderDef, model: str, trace: trace_mod.RequestTrace,
    api_key_id: str | None, stream: bool,
) -> AsyncIterator[dict[str, Any]]:
    """
    Send through connection candidates.

    Yields OpenAI-shaped chunks, or one dict {'__error__': True, ...} when
    every option is exhausted. Final usage is attached to the trace.

    """
    provider = registry.get_provider(definition.id)
    excluded: set[str] = set()
    attempts = 0
    max_attempts = max(1, settings.get_int("routing.max_connections_per_request"))
    deadline = time.monotonic() + config.UPSTREAM_TOTAL_TIMEOUT_S
    usage_acc: dict[str, Any] = {}
    signals_acc: dict[str, Any] = {}
    trace.stage(trace_mod.STEP_OPENAI_MID, {**payload, "model": model, "stream": stream}, direction="out")

    while True:
        chosen, skipped, plan = selector.pick(definition, model, exclude=excluded)
        selector.log_plan(plan, chosen, skipped, rid=trace.request_id)
        if chosen is None:
            reason = skipped[0]["reason"] if skipped else "this provider has no connections yet"
            message = f"no healthy connection: {reason}"
            yield {"__error__": True, "message": message, "code": errors.CLASS_ALL_LOCKED,
                   "status": 503, "upstream_status": None, "vendor_code": None}
            return

        trace.bind_connection(chosen.credentials.connection_id)
        request = provider.build_request({**payload, "model": model}, chosen.credentials, stream=stream)
        trace.tier = request.tier
        trace.stage(trace_mod.STEP_PROVIDER_OUT,
                    {"url": request.url, "method": request.method,
                     "headers": applog.redact_headers(request.headers),
                                                 "body": request.body,
                                                 "wire": {"url": request.url, "query": request.query,
                                                            "body_bytes": len(request.body_bytes or b"")}},
                    direction="out")
        selector.mark_selected(chosen, sticky=plan["strategy"] == selector.STRATEGY_ROUND_ROBIN)
        reservation = budget.reserve(provider=definition.id, api_key_id=api_key_id,
                                     est_tokens=estimate_prompt_tokens(payload))
        attempts += 1
        if config.DRY_RUN:
            # Dry-run: payload udah dibangun + ke-trace, upstream gak pernah disentuh.
            # Ini jalur buat nge-diff request kita vs vendor tanpa bakar kuota.
            trace.dry_run = True
            budget.settle(reservation, 0)
            trace.stage(trace_mod.STEP_DRY_RUN, {"upstream_called": False,
                                                 "reason": "EROUTER_DRY_RUN",
                                                 "url": request.url}, direction="out")
            estimate = usage_mod.with_estimated_completion(
                usage_mod.estimate_from_body(payload), 0)
            yield {"id": f"chatcmpl-{trace.request_id}", "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                   "usage": usage_mod.as_client_usage_payload(estimate),
                   "engrix_dry_run": True}
            return
        got_first = False
        failure: Any = None
        try:
            if stream:
                async for chunk in provider.open_stream(request):
                    if not got_first:
                        got_first = True
                        trace.stage(trace_mod.STEP_UPSTREAM_IN, chunk, direction="in")
                    found = usage_mod.extract(chunk)
                    if found:
                        usage_acc = usage_mod.merge(usage_acc, found)
                    signals_acc = usage_mod.merge_signals(
                        signals_acc, usage_mod.vendor_signals(chunk.get("usage")))
                    content_len = _delta_content_len(chunk)
                    reasoning_len = _delta_reasoning_len(chunk)
                    # # ttft = token KLIEN pertama yang benar-benar terlihat
                    # # (konten atau thinking). Frame kosong (message_start/ping/
                    # # usage-only) dulu ikut nge-set TTFT, jadi angka 605ms itu
                    # # RTT handshake — bukan latency model. Yang ngukur RTT murni
                    # # tetap trace.stage UPSTREAM_IN.
                    if content_len or reasoning_len:
                        trace.note_first_token()
                    trace.note_frame(content_len, reasoning_chars=reasoning_len)
                    yield chunk
            else:
                response = await provider.complete(request)
                got_first = True
                trace.stage(trace_mod.STEP_UPSTREAM_IN, response, direction="in")
                found = usage_mod.extract(response)
                if found:
                    usage_acc = usage_mod.merge(usage_acc, found)
                signals_acc = usage_mod.merge_signals(
                    signals_acc, usage_mod.vendor_signals(response.get("usage")))
                trace.note_first_token()
                chunk0 = _completion_as_chunk(response, model=model)
                trace.note_frame(_delta_content_len(chunk0),
                                 reasoning_chars=_delta_reasoning_len(chunk0))
                yield _completion_as_chunk(response, model=str(payload.get("model") or model))
            trace.attach_usage(usage_acc, signals=signals_acc)
            budget.settle(reservation, int(usage_acc.get("total") or 0))
            health.register_success(chosen.credentials.connection_id)
            return
        except UpstreamError as exc:
            failure = exc
            classified = errors.classify(status=exc.status, text=str(exc), vendor_code=exc.vendor_code,
                                         exception_name=exc.kind)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            failure = exc
            classified = errors.classify(text=str(exc) or "timeout", exception_name="TimeoutError")
        except Exception as exc:  # transport & hal tak terduga
            failure = exc
            classified = errors.classify(text=f"{exc.__class__.__name__}: {exc}",
                                         exception_name=exc.__class__.__name__)
            if classified.error_class == errors.CLASS_UNKNOWN:
                classified = errors.Classified(
                    errors.CLASS_UPSTREAM_UNAVAILABLE,
                    errors.policy_of(errors.CLASS_UPSTREAM_UNAVAILABLE),
                    f"{exc.__class__.__name__}: {exc}"[:400],
                )

        decision = health.register_error(chosen.credentials.connection_id, model, classified)
        applog.request_failed(
            rid=trace.request_id, provider=definition.id, model=model,
            total_ms=now_ms() - trace.started_ms, error_class=classified.error_class,
            http_out=classified.client_status, vendor_code=classified.vendor_code,
            account=chosen.label, message=classified.message,
            lock_ms=decision.get("lock_ms"),
            retry_after_s=classified.retry_after_effective(settings.get_int("client.retry_after_default_s")),
        )
        budget.settle(reservation, 0)
        excluded.add(chosen.credentials.connection_id)

        stop_reason = None
        if got_first:
            stop_reason = "bytes were already sent to the client"
        elif not classified.policy.retry_internally:
            stop_reason = f"class {classified.error_class} must not be retried internally"
        elif attempts >= max_attempts:
            stop_reason = f"attempt cap of {max_attempts} reached"
        elif time.monotonic() > deadline:
            stop_reason = "gateway deadline reached"
        if stop_reason:
            yield {"__error__": True,
                   "message": f"{classified.message[:500]} ({stop_reason})",
                   "code": classified.error_class, "status": classified.client_status,
                   "upstream_status": getattr(failure, "status", None),
                   "vendor_code": classified.vendor_code}
            return
        delay = _sleep_before_retry(classified, definition)
        if delay:
            await asyncio.sleep(delay)


async def run_chat(body: dict[str, Any], *, api_key_id: str | None = None,
                   lane: str = "interactive",
                   endpoint: str = "/v1/chat/completions") -> dict[str, Any]:
    """Non-stream path. Raises RequestRejected when refused or out of options."""
    definition, model = _resolve(body)
    trace = trace_mod.begin(endpoint, api_key_id=api_key_id, budget_lane=lane)
    trace.bind_target(provider=definition.id, model=model)
    trace.stage(trace_mod.STEP_CLIENT_IN, body, direction="in")
    await _admission(provider=definition.id, api_key_id=api_key_id, body=body, lane=lane)

    chunks: list[dict[str, Any]] = []
    async for frame in _dispatch(payload=dict(body), definition=definition, model=model, trace=trace,
                                 api_key_id=api_key_id, stream=False):
        if frame.get("__error__"):
            trace.finish(status=trace_mod.STATUS_UPSTREAM_ERROR, http_out=int(frame["status"]),
                         error_class=str(frame["code"]), error_text=str(frame["message"]),
                         upstream_status=frame.get("upstream_status"), error_code=frame.get("vendor_code"))
            raise RequestRejected(
                errors.classify(status=int(frame["status"]), text=str(frame["message"]),
                                vendor_code=frame.get("vendor_code"),
                                exception_name=str(frame.get("code"))))
        chunks.append(frame)
    usage = trace.usage or usage_mod.estimate_from_body(body)
    completion = openai_fmt.to_completion(trace.request_id, str(body.get("model") or model), chunks,
                                       usage_mod.as_client_usage_payload(usage))
    if trace.dry_run:
        completion["engrix_dry_run"] = True
    result = trace.finish(status=trace_mod.STATUS_DRY_RUN if trace.dry_run else trace_mod.STATUS_OK,
                          http_out=200, usage=usage,
                          usage_source="upstream" if trace.usage else "estimated")
    _log_done(trace, result)
    return completion


async def stream_frames(body: dict[str, Any], *, api_key_id: str | None = None,
                        lane: str = "interactive", endpoint: str = "/v1/chat/completions",
                        render, request_id: str | None = None) -> AsyncIterator[dict[str, Any]]:
    """
    The canonical frame stream every inbound format is rendered from.

    One pipeline, N wire formats (ADR-0001): this yields frames, `api/openai.py` turns
    them into OpenAI SSE and `api/anthropic.py` into Anthropic events. `render` is
    passed in only so the trace can store the exact bytes the client saw instead of a
    re-serialization that could drift from what was sent.

    Never raises: a failure becomes a `{"__error__": True, ...}` frame.
    """
    try:
        definition, model = _resolve(body)
    except RequestRejected as exc:
        yield {"__error__": True, "message": exc.message, "code": exc.classified.error_class,
               "status": exc.classified.client_status, "upstream_status": None,
               "vendor_code": exc.classified.vendor_code}
        return
    trace = trace_mod.begin(endpoint, api_key_id=api_key_id, budget_lane=lane,
                            request_id=request_id)
    trace.bind_target(provider=definition.id, model=model)
    trace.stage(trace_mod.STEP_CLIENT_IN, body, direction="in")
    try:
        await _admission(provider=definition.id, api_key_id=api_key_id, body=body, lane=lane)
    except RequestRejected as exc:
        status = exc.classified.client_status
        trace.finish(status=trace_mod.STATUS_BUDGET if status == 429 else trace_mod.STATUS_CLIENT_ERROR,
                     http_out=status, error_class=exc.classified.error_class, error_text=exc.message)
        yield {"__error__": True, "message": exc.message, "code": exc.classified.error_class,
               "status": status, "upstream_status": None, "vendor_code": exc.classified.vendor_code}
        return
    created = int(time.time())
    client_model = str(body.get("model") or model)
    finish_seen: str | None = None
    emitted: list[str] = []
    async for frame in _dispatch(payload=dict(body), definition=definition, model=model, trace=trace,
                                 api_key_id=api_key_id, stream=True):
        if frame.get("__error__"):
            status = int(frame.get("status") or 502)
            emitted.append(render({"error": frame}))
            trace.stage(trace_mod.STEP_CLIENT_OUT, _tail(emitted), direction="out")
            trace.finish(status=trace_mod.STATUS_UPSTREAM_ERROR, http_out=status,
                         error_class=str(frame.get("code")), error_text=str(frame.get("message")),
                         upstream_status=frame.get("upstream_status"), error_code=frame.get("vendor_code"))
            yield frame
            return
        for choice in frame.get("choices") or []:
            if choice.get("finish_reason"):
                finish_seen = openai_fmt.normalize_finish_reason(choice["finish_reason"]) or finish_seen
        frame = _chunk_for_client(frame, request_id=trace.request_id, model=client_model, created=created)
        emitted.append(render(frame))
        yield frame

    usage = trace.usage or _estimated_usage(body, trace)
    trailer = openai_fmt.chunk(rid=trace.request_id, model=client_model, created=created, delta={},
                             finish_reason=finish_seen or "stop",
                             usage=usage_mod.as_client_usage_payload(usage))
    emitted.append(render(trailer))
    yield trailer
    trace.stage(trace_mod.STEP_CLIENT_OUT, _tail(emitted), direction="out")
    result = trace.finish(status=trace_mod.STATUS_DRY_RUN if trace.dry_run else trace_mod.STATUS_OK,
                          http_out=200, usage=usage,
                          usage_source="upstream" if trace.usage else "estimated")
    _log_done(trace, result)


async def stream_chat(body: dict[str, Any], *, api_key_id: str | None = None, lane: str = "interactive",
                      endpoint: str = "/v1/chat/completions") -> AsyncIterator[str]:
    """Streaming path in the OpenAI wire format: yields SSE strings, ends with [DONE]."""
    async for frame in stream_frames(body, api_key_id=api_key_id, lane=lane, endpoint=endpoint,
                                     render=openai_fmt.sse):
        if frame.get("__error__"):
            yield openai_fmt.error_frame(str(frame.get("message") or "upstream error"),
                                       code=str(frame.get("code") or errors.CLASS_UNKNOWN),
                                       status=int(frame.get("status") or 502))
            return
        yield openai_fmt.sse(frame)
    yield openai_fmt.SSE_DONE


def _tail(lines: list[str], limit: int = 4000) -> str:
    return "".join(lines)[-limit:]


def _estimated_usage(body: dict[str, Any], trace: trace_mod.RequestTrace) -> dict[str, int]:
    """
    Used ONLY when the upstream sent no usage; flagged via usage_source.

    Not 9router's +2000 buffer (open-sse/utils/usageTracking.js:21), which is
    what inflated the engrix ledger by 2,000 tokens per call.

    """
    estimate = usage_mod.estimate_from_body(body)
    return usage_mod.with_estimated_completion(estimate, trace.seen_content_chars // 4)


def _log_done(trace: trace_mod.RequestTrace, result: dict[str, Any]) -> None:
    usage = result.get("usage") or {}
    applog.request_done(
        rid=trace.request_id, total_ms=int(result.get("total_ms") or 0), ttft_ms=trace.ttft_ms,
        prompt=int(usage.get("prompt") or 0), cached=int(usage.get("cached") or 0),
        cache_creation=int(usage.get("cache_creation") or 0),
        completion=int(usage.get("completion") or 0), reasoning=int(usage.get("reasoning") or 0),
        cost_usd=float(result.get("cost_usd") or 0.0), frames=trace.frames,
        status=str(result.get("status") or "done").upper(),
    )
