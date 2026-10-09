"""
The Anthropic Messages format: request in, response out, SSE events rendered.

Why this lives in `core/formats/` and not in `api/`: the same translation is needed
by two opposite directions and they must not drift apart --

* inbound: a client calls `POST /v1/messages` with an Anthropic body and the gateway
  answers in Anthropic shape, while the pipeline in between only speaks canonical;
* outbound: an Anthropic-compatible upstream speaks that shape natively, and the
  provider has to fold its answer back into canonical frames.

`to_canonical_body()` and `from_canonical_*()` are inverses of each other, tested
against each other (tests/test_anthropic_format.py), so a field handled for one
direction cannot silently be missing for the other.

Rules kept from ADR-0001:
  * fields we do not recognise are PASSED THROUGH, not dropped - the gateway is a
    translator, so the client must be able to set a knob the vendor understands
    even if we have never heard of it;
  * `thinking` deltas stay separate from text (a reasoning block is its own content
    block of type "thinking"; folding it into text corrupts both the answer and the
    token accounting);
  * tool arguments are re-emitted as `input_json_delta` fragments, because that is
    the only way an Anthropic client can stream-parse them.
"""
from __future__ import annotations

import json
from typing import Any

ANTHROPIC_VERSION_DEFAULT = "2023-06-01"

# Canonical finish_reason -> stop_reason Anthropic. `content_filter` gak punya padanan di Anthropic,
# makanya end_turn dipakai -- itu yang dikirim vendor sendiri pas dia stop duluan.
_STOP_REASON = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "end_turn",
    "error": "end_turn",
}

_BLOCK_TOOLS = "tool_use"

# ...dan arah baliknya (outbound: vendor -> canonical).
_FINISH_TO_STOP = {"end_turn": "stop", "max_tokens": "length", "stop_sequence": "stop",
                   "tool_use": "tool_calls", "refusal": "content_filter"}


def to_stop_reason(finish_reason: str | None) -> str:
    return _STOP_REASON.get(finish_reason or "stop", "end_turn")


def _blocks_to_text_and_tools(content: Any) -> tuple[str, list[dict[str, Any]], str]:
    """Flatten one Anthropic `content` value into (text, tool_use calls, thinking text)."""
    if isinstance(content, str):
        return content, [], ""
    text: list[str] = []
    tools: list[dict[str, Any]] = []
    thinking: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            text.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text") or ""))
        elif kind == "thinking":
            thinking.append(str(block.get("thinking") or ""))
        elif kind == _BLOCK_TOOLS:
            tools.append({"id": block.get("id"), "type": "function", "function": {
                "name": block.get("name") or "",
                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
        else:
            text.append(json.dumps(block, ensure_ascii=False))
    return "".join(text), tools, "".join(thinking)


def _tool_result_to_canonical(block: dict[str, Any]) -> dict[str, Any]:
    content = block.get("content")
    if isinstance(content, list):
        content = "".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    elif content is not None and not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": block.get("tool_use_id") or "",
            "content": content if content is not None else ""}


def to_canonical_body(body: dict[str, Any]) -> dict[str, Any]:
    """
    Anthropic Messages request -> the canonical body the pipeline speaks.

    `system` is hoisted into a leading system message (Anthropic keeps it outside the
    message list, OpenAI inside); `tool_result` blocks become role=tool messages,
    which is the only place a client's tool output can be routed back to a call id.
    """
    out: dict[str, Any] = {key: value for key, value in body.items()
                           if key not in {"model", "messages", "system", "tools",
                                          "tool_choice", "stop_sequences"}}
    out["model"] = body.get("model")
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]

    messages: list[dict[str, Any]] = []
    system = body.get("system")
    if isinstance(system, list):
        system = "".join(str(b.get("text") or "") for b in system if isinstance(b, dict))
    if system:
        messages.append({"role": "system", "content": str(system)})

    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            results = [_tool_result_to_canonical(b) for b in content
                       if isinstance(b, dict) and b.get("type") == "tool_result"]
            text, tools, thinking = _blocks_to_text_and_tools(
                [b for b in content if not (isinstance(b, dict) and b.get("type") == "tool_result")])
            if results:
                messages.extend(results)
            converted: dict[str, Any] = {"role": role, "content": text}
            if thinking:
                converted["reasoning_content"] = thinking
            if tools:
                converted["tool_calls"] = tools
            messages.append(converted)
        else:
            messages.append({"role": role, "content": content})
    out["messages"] = messages

    if isinstance(body.get("tools"), list):
        out["tools"] = [{"type": "function", "function": {
            "name": tool.get("name"), "description": tool.get("description") or "",
            "parameters": tool.get("input_schema") or {}}} for tool in body["tools"]]
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "any":
            out["tool_choice"] = "required"
        elif kind == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": choice.get("tool_name")}}
        elif kind in {"auto", "none"}:
            out["tool_choice"] = kind
    return out


def usage_from_canonical(usage: dict[str, Any] | None) -> dict[str, Any]:
    """
    Canonical usage -> Anthropic usage names (they are not the same words).

    Accepts the canonical short names AND the OpenAI wire names, because this is
    called with whatever the pipeline has on hand at that moment. `core` cannot import
    `subscribers.usage` (wrong direction in the layer map), so the tolerance lives here.
    """
    clean = usage or {}

    def pick(*names: str) -> int:
        for name in names:
            value = clean.get(name)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return 0
        return 0

    out: dict[str, Any] = {"input_tokens": pick("prompt", "prompt_tokens", "input_tokens"),
                           "output_tokens": pick("completion", "completion_tokens", "output_tokens")}
    cached = pick("cached", "cached_tokens", "cache_read_input_tokens")
    creation = pick("cache_creation", "cache_creation_input_tokens")
    if cached:
        out["cache_read_input_tokens"] = cached
    if creation:
        out["cache_creation_input_tokens"] = creation
    return out


def content_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """OpenAI assistant message -> Anthropic content blocks (thinking, text, tool_use)."""
    blocks: list[dict[str, Any]] = []
    if message.get("reasoning_content") or message.get("reasoning"):
        blocks.append({"type": "thinking", "thinking": str(message.get("reasoning_content")
                                                           or message.get("reasoning"))})
    content = message.get("content")
    if content:
        blocks.append({"type": "text", "text": str(content)})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            parsed = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            parsed = {"_raw": function.get("arguments")}
        blocks.append({"type": _BLOCK_TOOLS, "id": call.get("id"), "name": function.get("name") or "",
                       "input": parsed})
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def from_canonical_completion(request_id: str, model: str, completion: dict[str, Any]) -> dict[str, Any]:
    """chat.completion -> Anthropic `message` object (the non-stream response)."""
    message = ((completion.get("choices") or [{}])[0].get("message") or {})
    finish = completion.get("finish_reason") or (completion.get("choices") or [{}])[0].get("finish_reason")
    return {
        "id": f"msg_{request_id}",
        "type": "message",
        "role": "assistant",
        "model": completion.get("model") or model,
        "content": content_from_message(message),
        "stop_reason": to_stop_reason(finish),
        "stop_sequence": None,
        "usage": usage_from_canonical(completion.get("usage") or {}),
    }


def error_body(message: str, *, kind: str = "api_error", status: int = 500) -> dict[str, Any]:
    """Anthropic-shaped error object (a client SDK raises on `type`, not on our code)."""
    return {"type": "error", "error": {"type": kind, "message": message[:900], "status": status}}


def completion_from_message(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Anthropic `message` object -> canonical chat.completion (outbound non-stream).

    The inverse of from_canonical_completion(), and deliberately so: a field the
    inbound path emits must be readable by the outbound path, otherwise the two
    directions of the same format drift apart without anyone noticing.
    """
    text: list[str] = []
    reasoning: list[str] = []
    tools: list[dict[str, Any]] = []
    for block in payload.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text") or ""))
        elif kind == "thinking":
            reasoning.append(str(block.get("thinking") or ""))
        elif kind == _BLOCK_TOOLS:
            tools.append({"id": block.get("id"), "type": "function", "function": {
                "name": block.get("name") or "",
                "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False)}})
    finish = _FINISH_TO_STOP.get(str(payload.get("stop_reason") or "end_turn"), "stop")
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tools:
        message["tool_calls"] = tools
    usage = payload.get("usage") or {}
    return {
        "id": payload.get("id") or "",
        "object": "chat.completion",
        "created": 0,
        "model": payload.get("model") or "",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": int(usage.get("input_tokens") or 0),
                  "completion_tokens": int(usage.get("output_tokens") or 0),
                  "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
                  "cached_tokens": int(usage.get("cache_read_input_tokens") or 0),
                  "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0)},
    }


def sse_event(name: str, payload: dict[str, Any]) -> str:
    """Anthropic SSE carries `event:` AND `data:`; the OpenAI helper only sends data."""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


class StreamRenderer:
    """
    Canonical frames -> Anthropic message events. One instance per stream.

    Anthropic numbers content blocks and requires a block to be opened before it is
    written to and closed before the next one starts, so this is a state machine and
    not a per-frame mapping. A vendor that interleaves reasoning, text and tool
    arguments still produces a legal event sequence because of that.
    """

    def __init__(self, request_id: str, model: str) -> None:
        self.message_id = f"msg_{request_id}"
        self.model = model
        self.index = -1
        self.open_kind: str | None = None
        self.tool_position: int | None = None     # index tool_call yang lagi dibuka
        self.stop_reason = "end_turn"
        self.output_tokens = 0

    def start(self) -> str:
        return sse_event("message_start", {
            "type": "message_start",
            "message": {"id": self.message_id, "type": "message", "role": "assistant",
                        "model": self.model, "content": [], "stop_reason": None,
                        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _open(self, block: dict[str, Any]) -> list[str]:
        self.index += 1
        self.open_kind = str(block.get("type"))
        return [sse_event("content_block_start", {"type": "content_block_start",
                                                  "index": self.index, "content_block": block})]

    def _close(self) -> list[str]:
        if self.open_kind is None:
            return []
        self.open_kind = None
        return [sse_event("content_block_stop", {"type": "content_block_stop", "index": self.index})]

    def _delta(self, kind: str, payload: dict[str, str]) -> str:
        return sse_event("content_block_delta", {"type": "content_block_delta",
                                                 "index": self.index, "delta": {"type": kind, **payload}})

    def push(self, frame: dict[str, Any]) -> list[str]:
        """One canonical frame in, zero or more SSE lines out."""
        out: list[str] = []
        for choice in frame.get("choices") or []:
            delta = choice.get("delta") or {}
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                if self.open_kind != "thinking":
                    out += self._close() + self._open({"type": "thinking", "thinking": ""})
                out.append(self._delta("thinking_delta", {"thinking": str(reasoning)}))
            if delta.get("content"):
                if self.open_kind != "text":
                    out += self._close() + self._open({"type": "text", "text": ""})
                out.append(self._delta("text_delta", {"text": str(delta["content"])}))
                self.output_tokens += max(1, len(str(delta["content"])) // 4)
            for call in delta.get("tool_calls") or []:
                out += self._tool(call)
            if choice.get("finish_reason"):
                self.stop_reason = to_stop_reason(choice["finish_reason"])
        usage = frame.get("usage")
        if isinstance(usage, dict) and usage.get("completion_tokens") is not None:
            self.output_tokens = int(usage["completion_tokens"])
        return out

    def _tool(self, call: dict[str, Any]) -> list[str]:
        """Tool calls live in their own block: opened once, filled by partial JSON."""
        position = int(call.get("index") or 0)
        function = call.get("function") or {}
        out: list[str] = []
        if self.open_kind != _BLOCK_TOOLS or self.tool_position != position:
            # Pindah ke tool_call lain di tengah stream = blok baru, karena Anthropic
            # gak pernah mencampur dua tool_call dalam satu content block.
            out += self._close()
            self.tool_position = position
            out += self._open({"type": _BLOCK_TOOLS,
                               "id": call.get("id") or f"toolu_{self.message_id}_{position}",
                               "name": function.get("name") or "", "input": {}})
        arguments = function.get("arguments")
        if arguments:
            out.append(self._delta("input_json_delta", {"partial_json": str(arguments)}))
            self.output_tokens += max(1, len(str(arguments)) // 4)
        return out

    def finish(self) -> list[str]:
        out = self._close()
        out.append(sse_event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens}}))
        out.append(sse_event("message_stop", {"type": "message_stop"}))
        return out
