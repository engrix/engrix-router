"""
BaseProvider: the part every provider shares (URL, headers, SSE, errors).

The counterpart of 9router's executors/base.js, with one important difference:
the base here has NO retry ladder. Retries live in pipeline/runner.py so that
the decision exists in exactly one place. The reason is concrete: QoderExecutor
in 9router overrides execute() wholesale (open-sse/executors/qoder.js:613) and
silently drops base.js's ladder - that vendor got 0 retries and 0 URL fallbacks,
and nobody noticed for several versions. We do not want to be caught that way.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from urllib.parse import urlencode

from engrix_router.core import config
from engrix_router.core.types import (
    Credentials,
    ExecRequest,
    ModelSpec,
    ProbeResult,
    ProviderDef,
    QuotaReading,
    UpstreamError,
    UsageSpec,
)


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


class BaseProvider:
    definition: ProviderDef

    def __init__(self, definition: ProviderDef) -> None:
        # Dipisah dari OpenAICompatibleProvider supaya subclass baru (qoder/COSY)
        # gak perlu nginget-ngetik constructor lagi; dulu cuma subclass khusus
        # yang punya __init__, jadi BaseProvider(definition) langsung TypeError.
        self.definition = definition

    # ── build (murni, tanpa jaringan) ────────────────────────────────────────
    def transport(self):
        return self.definition.transport

    def chat_url(self) -> str:
        spec = self.transport()
        return join_url(spec.base_url, spec.chat_path) + (spec.url_suffix or "")

    def models_url(self) -> str:
        spec = self.transport()
        if spec.models_url:
            return spec.models_url
        return join_url(spec.base_url, spec.models_path)

    def build_headers(self, creds: Credentials, *, stream: bool, accept: str | None = None) -> dict[str, str]:
        spec = self.transport()
        headers = {
            "content-type": "application/json",
            "accept": accept or ("text/event-stream" if stream else "application/json"),
        }
        # Accept-Encoding eksplisit kalau provider minta. Alasannya konkret:
        # 9router maksa "identity" di qoder karena upstream gzip bikin CDN-nya
        # ngejalanin validasi signature (executors/qoder.js:700-701). httpx default
        # ngirim "gzip, deflate" -> request yang udah ditandatangani bisa ditolak
        # dengan error yang kelihatan kayak salah kredensial.
        if spec.accept_encoding:
            headers["accept-encoding"] = spec.accept_encoding
        headers.update({k.lower(): v for k, v in spec.headers.items()})
        # Nama header dinormalisasi lowercase di kita: HTTP case-insensitive,
        # tapi campuran 'content-type' + 'Authorization' bikin lookup dan redaksi
        # di trace/replay tools jadi jebakan. Vendor tetap dikirim apa adanya
        # karena httpx mengirim nama persis seperti di dict.
        headers.update({k.lower(): v for k, v in spec.auth.apply(creds.token).items()})
        return headers

    # ── wire layer: bentuk byte yang actually dikirim + tanda tangan di atasnya ──
    def encode_body(self, payload: dict[str, Any], *, stream: bool) -> tuple[bytes, dict[str, Any]]:
        """
        Return (body_bytes, query_extra). Default: UTF-8 JSON, no extra query.

        Providers that need body obfuscation (WAF bypass) override this. Qoder,
        for instance: JSON -> base64 -> thirds rearranged -> custom alphabet
        (open-sse/shared/qoder/encoding.js, ported from QoderEncoding.java), then
        it adds `?Encode=1` to query_extra. The rule that matters: produce the
        BYTES first, sign second - any signature or hash must be computed over
        exactly the bytes that go on the wire.

        """
        return (json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), {})

    def sign_request(self, request: ExecRequest, creds: Credentials) -> ExecRequest:
        """
        Last hook over the final body + URL. Default: does nothing.

        This is where a vendor like Qoder signs COSY: MD5(payloadB64, cosyKey,
        timestamp-in-seconds, body, sigPath) plus the Cosy-Bodyhash and
        Cosy-Bodylength headers computed from request.body_bytes. It runs per
        attempt (the runner calls build_request for every candidate), so
        requestId and timestamp are always new - required, because upstream
        punishes replay: `403 code 103 Duplicate request` in 9router's changelog
        (executors/qoder.js:714-717), a silent hang in our own probe.

        """
        return request

    def build_request(self, body: dict[str, Any], creds: Credentials, *, stream: bool) -> ExecRequest:
        spec = self.transport()
        payload = self.transform_request(body, creds=creds, stream=stream)
        body_bytes, query_extra = self.encode_body(payload, stream=stream)
        url = self.chat_url()
        if query_extra:
            url = f"{url}?{urlencode(query_extra)}"
        request = ExecRequest(
            url=url,
            headers=self.build_headers(creds, stream=stream),
            body=payload,
            body_bytes=body_bytes,
            query=dict(query_extra),
            connect_timeout_s=spec.connect_timeout_s or config.UPSTREAM_CONNECT_TIMEOUT_S,
            first_chunk_timeout_s=config.UPSTREAM_FIRST_CHUNK_TIMEOUT_S,
            stall_timeout_s=spec.stall_timeout_s or config.UPSTREAM_STALL_TIMEOUT_S,
            total_timeout_s=config.UPSTREAM_TOTAL_TIMEOUT_S,
            proxy_url=creds.proxy_url,
        )
        return self.sign_request(request, creds)

    def transform_request(self, body: dict[str, Any], *, creds: Credentials,
                          stream: bool) -> dict[str, Any]:
        """
        Default: pass everything through, set stream, drop internal keys.

        `creds` is passed in (rather than stored on self) because one provider
        instance is shared by every concurrent request: a provider keeping
        connection_id or session state on self would overwrite another account
        the moment two accounts are called together - and round-robin is the
        default. Vendors that need credentials to build a payload (qoder: user_id
        for COSY, connection_id for the model catalog) get them through this
        argument.

        """
        payload = {k: v for k, v in body.items() if not k.startswith("_")}
        payload["stream"] = bool(stream)
        for key in getattr(self, "strip_params", ()):
            payload.pop(key, None)
        if stream and self.transport().send_stream_options:
            payload.setdefault("stream_options", {"include_usage": True})
        return payload

    # ── pemetaan error di level transport ────────────────────────────────────
    @staticmethod
    def describe_http_error(status: int, text: str) -> UpstreamError:
        return UpstreamError(text or f"upstream HTTP {status}", status=status)

    def check_response(self, response: httpx.Response, text: str) -> None:
        if response.status_code >= 400:
            raise self.describe_http_error(response.status_code, text[:2000])
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and "text/event-stream" not in content_type and "json" not in content_type:
            # Padanan blokir non-SSE di 9router (chatCore/streamingHandler.js:64-82):
            # login page / HTML error dari CDN jangan sampai kita stream ke klien.
            snippet = (text[:300] if len(text) < 4096 else "") or content_type
            raise UpstreamError(f"upstream mengirim content-type {content_type}: {snippet}",
                                status=response.status_code, kind="transport")

    # ── satu percobaan kirim ─────────────────────────────────────────────────
    def unwrap_data(self, data: str) -> dict[str, Any] | None:
        """
        Return an OpenAI-shaped chunk from one raw `data:` line. Default: plain JSON.

        The inverse of encode_body(): a vendor that wraps every frame in an
        envelope owns its parser in its own file. Qoder sends
        `{"headers":..., "body":"<chunk JSON as a string>", "statusCodeValue":200}`
        - and an error inside that envelope (statusCodeValue != 200) must become
        an error, not an empty chunk that makes the client think the model went
        quiet.

        """
        return _safe_json(data)

    async def open_stream(self, request: ExecRequest) -> AsyncIterator[dict[str, Any]]:
        try:
            async for chunk in self._open_stream_inner(request):
                yield chunk
        except httpx.HTTPError as exc:
            raise UpstreamError(
                f"transport error: {exc.__class__.__name__}: {exc}", kind="transport"
            ) from exc

    async def _open_stream_inner(self, request: ExecRequest) -> AsyncIterator[dict[str, Any]]:
        """
        Yield OpenAI-shaped chunks. Timeouts: first chunk, then stall per data line.

        The watchdog sits BELOW parsing (per raw chunk), like 9router's
        pipeWithDisconnect tapping raw upstream bytes
        (open-sse/utils/streamHandler.js:195-248) - not on deltas we already
        emitted, so a long silent reasoning phase is not mistaken for a dead
        stream.

        """
        from engrix_router.transport.http_client import get_client

        client = await get_client(request.proxy_url)
        async with client.stream(
            request.method,
            request.url,
            headers=request.headers,
            content=request.body_bytes,
            timeout=httpx.Timeout(
                request.total_timeout_s,
                connect=request.connect_timeout_s,
                read=request.stall_timeout_s,
            ),
        ) as response:
            if response.status_code >= 400:
                text = (await response.aread()).decode("utf-8", "replace")
                raise self.describe_http_error(response.status_code, text[:2000])
            content_type = (response.headers.get("content-type") or "").lower()
            if "text/event-stream" not in content_type:
                text = (await response.aread()).decode("utf-8", "replace")
                self.check_response(response, text)
                # upstream jawab JSON ke streaming request: perlakukan 1 chunk.
                yield _safe_json(text) or {"raw": text[:2000]}
                return

            lines = response.aiter_lines()
            first = True
            while True:
                deadline = request.first_chunk_timeout_s if first else request.stall_timeout_s
                try:
                    async with asyncio.timeout(deadline):
                        line = await lines.__anext__()
                except asyncio.TimeoutError as exc:
                    raise UpstreamError(
                        f"{'first chunk' if first else 'stream'} timeout setelah {deadline:.0f}s",
                        kind="timeout",
                    ) from exc
                except StopAsyncIteration:
                    return
                if not line:
                    continue
                if line.startswith(":"):
                    continue  # komentar SSE / keepalive
                first = False
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                chunk = self.unwrap_data(data)
                if chunk is None:
                    continue
                error = chunk.get("error") if isinstance(chunk, dict) else None
                if isinstance(error, dict):
                    # 9router sengaja mengirim frame error + [DONE] dan MENOLAK
                    # memalsukan finish_reason (open-sse/utils/streamHelpers.js:130-147).
                    # Kita teruskan sebagai error biar SDK klien raise.
                    raise UpstreamError(str(error.get("message") or error)[:2000], kind="http")
                yield chunk

    async def complete(self, request: ExecRequest) -> dict[str, Any]:
        from engrix_router.transport.http_client import get_client

        client = await get_client(request.proxy_url)
        try:
            async with asyncio.timeout(request.total_timeout_s):
                response = await client.request(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=request.body_bytes,
                )
        except asyncio.TimeoutError as exc:
            raise UpstreamError(f"upstream timeout setelah {request.total_timeout_s:.0f}s",
                                kind="timeout") from exc
        except httpx.HTTPError as exc:
            raise UpstreamError(f"transport error: {exc.__class__.__name__}: {exc}", kind="transport") from exc
        text = response.text
        self.check_response(response, text)
        payload = _safe_json(text)
        if payload is None:
            raise UpstreamError(f"upstream mengirim non-JSON: {text[:300]}", status=response.status_code)
        if isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise UpstreamError(str(error.get("message") or error)[:2000], status=response.status_code)
        return payload

    # ── introspeksi provider ─────────────────────────────────────────────────
    async def list_models(self, creds: Credentials) -> list[ModelSpec]:
        from engrix_router.transport.http_client import get_client

        client = await get_client(creds.proxy_url)
        headers = self.build_headers(creds, stream=False, accept="application/json")
        response = await client.get(self.models_url(), headers=headers,
                                    timeout=httpx.Timeout(20.0, connect=10.0))
        if response.status_code >= 400:
            raise self.describe_http_error(response.status_code, response.text[:1500])
        data = response.json()
        entries = data.get("data") if isinstance(data, dict) else data
        models: list[ModelSpec] = []
        for entry in entries or []:
            if isinstance(entry, str):
                models.append(ModelSpec(id=entry))
            elif isinstance(entry, dict) and entry.get("id"):
                models.append(
                    ModelSpec(
                        id=str(entry["id"]),
                        name=str(entry.get("name") or entry["id"]),
                        context_length=_int_or_none(entry.get("context_length") or entry.get("max_input_tokens")),
                        max_output=_int_or_none(entry.get("max_output_tokens")),
                    )
                )
        return models

    async def probe(self, creds: Credentials, model_id: str | None = None) -> ProbeResult:
        """
        models_list tier: cheap, costs no tokens. Other providers may override.

        This answers the hole where 9router returns 'Provider test not supported'
        for anything not in its giant switch (src/shared/services/testUtils.js:866).
        Here probe always has a default, so a new provider is debuggable on day one.

        """
        import time as _time

        started = _time.perf_counter()
        try:
            models = await self.list_models(creds)
        except UpstreamError as exc:
            return ProbeResult(ok=False, tier="models_list", error=str(exc)[:400],
                               latency_ms=int((_time.perf_counter() - started) * 1000))
        except Exception as exc:  # pragma: no cover - jalur tak terduga
            return ProbeResult(ok=False, tier="models_list", error=f"{exc.__class__.__name__}: {exc}"[:400])
        return ProbeResult(
            ok=True,
            tier="models_list",
            status=200,
            latency_ms=int((_time.perf_counter() - started) * 1000),
            model_probed=models[0].id if models else (model_id or self.transport().default_model),
            detail={"models_seen": len(models)},
        )

    async def fetch_quota(self, creds: Credentials) -> list[QuotaReading]:
        spec: UsageSpec | None = self.transport().usage
        if spec is None or not creds.token:
            return []
        from engrix_router.transport.http_client import get_client

        client = await get_client(creds.proxy_url)
        headers = spec.auth.apply(creds.token)
        response = await client.request(spec.method, spec.url, headers=headers,
                                        timeout=httpx.Timeout(20.0, connect=10.0))
        if response.status_code >= 400:
            raise self.describe_http_error(response.status_code, response.text[:1000])
        payload = response.json()
        readings: list[QuotaReading] = []
        for scope, path in (spec.mapping or {}).items():
            value = _dig(payload, path)
            readings.append(QuotaReading(scope=scope, used=_float_or_none(value), raw={"path": path}))
        if spec.reset_path:
            reset = _dig(payload, spec.reset_path)
            for reading in readings:
                reading.reset_at_ms = _as_epoch_ms(reset)
        return readings


def _safe_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else {"data": parsed}


def _dig(payload: Any, dotted: str) -> Any:
    current = payload
    for part in dotted.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_epoch_ms(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 1e11 else int(value * 1000)
    text = str(value)
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)
