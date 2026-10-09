"""
Generic Anthropic-compatible provider: `/v1/messages` upstreams.

Same rule as every other provider here -- one file owns one protocol. The wire shape
differs from OpenAI in four ways that actually matter, and each one is handled in one
place:

  * `max_tokens` is MANDATORY upstream (a request without it is a 400), while OpenAI
    clients often omit it -> a documented default is filled in, not invented per model.
  * `system` lives outside the message list -> hoisted out of the canonical messages.
  * auth is `x-api-key` + `anthropic-version` headers, not a bearer token -> declared
    in AuthSpec, so no service code branches on the vendor.
  * the stream is a sequence of typed events (message_start, content_block_*,
    message_delta, message_stop) instead of one chunk shape -> translated per event in
    unwrap_data(), back into canonical frames.

Tool-call translation needs no per-stream state on purpose: Anthropic's
`content_block_index` is already stable and monotonic inside one message, so it can be
used as the OpenAI `tool_calls[].index`. A provider instance is a singleton shared by
every concurrent request (see base.transform_request), so anything kept on `self` would
mix two accounts' streams.
"""
from __future__ import annotations

import json
from typing import Any

from engrix_router.core.formats import anthropic as fmt
from engrix_router.core.types import (
    AUTH_API_KEY_HEADER,
    AuthSpec,
    Credentials,
    ProviderDef,
    TransportSpec,
)
from engrix_router.providers.base import BaseProvider

_DEFAULT_MAX_TOKENS = 4096
_ANTHROPIC_VERSION = "2023-06-01"


def _to_native(body: dict[str, Any]) -> dict[str, Any]:
    """Canonical (OpenAI-shaped) body -> Anthropic Messages request."""
    messages = body.get("messages") or []
    system_parts = [str(m.get("content") or "") for m in messages
                    if isinstance(m, dict) and m.get("role") == "system"]
    out: dict[str, Any] = {key: value for key, value in body.items()
                           if key not in {"messages", "model", "stop", "tools",
                                          "tool_choice", "stream_options"}}
    out["model"] = body.get("model")
    if system_parts:
        out["system"] = "\n\n".join(part for part in system_parts if part)
    out["messages"] = _native_messages([m for m in messages
                                        if not (isinstance(m, dict) and m.get("role") == "system")])
    out.setdefault("max_tokens", _DEFAULT_MAX_TOKENS)
    if body.get("stop"):
        out["stop_sequences"] = body["stop"]
    if isinstance(body.get("tools"), list):
        out["tools"] = [{"name": (tool.get("function") or {}).get("name"),
                         "description": (tool.get("function") or {}).get("description") or "",
                         "input_schema": (tool.get("function") or {}).get("parameters") or {}}
                        for tool in body["tools"]]
    choice = body.get("tool_choice")
    if choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif isinstance(choice, dict) and choice.get("function"):
        out["tool_choice"] = {"type": "tool",
                              "tool_name": choice["function"].get("name")}
    elif choice in {"auto", "none"}:
        out["tool_choice"] = choice
    return out


def _native_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        if role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": message.get("tool_call_id") or "",
                "content": message.get("content") if isinstance(message.get("content"), str)
                else json.dumps(message.get("content"), ensure_ascii=False)}]})
            continue
        blocks: list[dict[str, Any]] = []
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if reasoning:
            blocks.append({"type": "thinking", "thinking": str(reasoning)})
        content = message.get("content")
        if content:
            blocks.append({"type": "text", "text": str(content)})
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            try:
                parsed = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                parsed = {}
            blocks.append({"type": "tool_use", "id": call.get("id"),
                           "name": function.get("name") or "", "input": parsed})
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        out.append({"role": role, "content": blocks})
    return out


class AnthropicProvider(BaseProvider):
    def transform_request(self, body: dict[str, Any], *, creds: Credentials,
                          stream: bool) -> dict[str, Any]:
        payload = _to_native({k: v for k, v in body.items() if not str(k).startswith("_")})
        payload["stream"] = bool(stream)
        model = str(payload.get("model") or "")
        if "/" in model:
            payload["model"] = model.split("/", 1)[1]
        return payload

    def unwrap_data(self, data: str) -> dict[str, Any] | None:
        """One Anthropic event -> one canonical frame (None = nothing to forward)."""
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if isinstance(event.get("error"), dict):
            # base._open_stream_inner mengubah frame `error` jadi UpstreamError;
            # kembalikan apa adanya, JANGAN di-drop sebagai event tak dikenal.
            return event
        if kind == "message_start":
            message = event.get("message") or {}
            return {"id": message.get("id") or "", "object": "chat.completion.chunk",
                    "created": 0, "model": message.get("model") or "",
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                    "usage": message.get("usage") or {}}
        if kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") != "tool_use":
                return None
            return _tool_frame(index=int(event.get("index") or 0), call_id=block.get("id"),
                               name=block.get("name") or "", arguments="")
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                return _chunk(delta={"content": delta.get("text") or ""})
            if dtype == "thinking_delta":
                return _chunk(delta={"reasoning_content": delta.get("thinking") or ""})
            if dtype == "input_json_delta":
                return _tool_frame(index=int(event.get("index") or 0), arguments
                                   =delta.get("partial_json") or "")
            return None
        if kind == "message_delta":
            from engrix_router.core.formats.openai import normalize_finish_reason

            stop = (event.get("delta") or {}).get("stop_reason")
            finish = normalize_finish_reason(_STOP_TO_FINISH.get(stop or "end_turn", "stop"))
            return {"id": "", "object": "chat.completion.chunk", "created": 0, "model": "",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                    "usage": event.get("usage") or {}}
        if kind == "message_stop":
            return None
        # ping / event yang belum kita kenal: di-drop, bukan dikirim sebagai chunk kosong
        return None

    async def complete(self, request: Any) -> dict[str, Any]:
        """Non-stream: the vendor answers a `message` object, the gateway speaks chat.completion."""
        payload = await super().complete(request)
        return fmt.completion_from_message(payload)


_STOP_TO_FINISH = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop",
                   "tool_use": "tool_calls", "refusal": "content_filter"}


def _chunk(*, delta: dict[str, Any], finish_reason: str | None = None,
           usage: dict[str, Any] | None = None) -> dict[str, Any]:
    frame: dict[str, Any] = {"id": "", "object": "chat.completion.chunk", "created": 0,
                             "model": "",
                             "choices": [{"index": 0, "delta": delta,
                                          "finish_reason": finish_reason}]}
    if usage:
        frame["usage"] = usage
    return frame


def _tool_frame(*, index: int, call_id: str | None = None, name: str = "",
                arguments: str = "") -> dict[str, Any]:
    call: dict[str, Any] = {"index": index, "type": "function",
                            "function": {"name": name, "arguments": arguments}}
    if call_id:
        call["id"] = call_id
    return _chunk(delta={"tool_calls": [call]})


def _definitions() -> tuple[ProviderDef, ...]:
    return (
        ProviderDef(
            id="anthropic",
            category="apikey",
            display_name="Anthropic",
            aliases=("anth",),
            auth_modes=("apikey",),
            transport=TransportSpec(
                base_url="https://api.anthropic.com/v1",
                chat_path="/messages",
                models_path="/models",
                auth=AuthSpec(kind=AUTH_API_KEY_HEADER, header="x-api-key", prefix="",
                              extra_headers={"anthropic-version": _ANTHROPIC_VERSION}),
                default_model="claude-sonnet-4-5",
                send_stream_options=False,
            ),
            probe_tier="models_list",
        ),
    )


DEFINITIONS = _definitions()
PROVIDER_CLASS = AnthropicProvider
