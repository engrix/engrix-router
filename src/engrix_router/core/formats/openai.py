"""
The OpenAI chunk contract that reaches the client.

This module exists even though most upstreams are already OpenAI-shaped:
the contract engrix-agent reads is specific, and breaking it makes that
client misdiagnose rather than merely render badly.
  * a stream that ends WITHOUT finish_reason counts as a stall, not success
    (engrix-agent/agent/streaming.py:1371, 1434-1441) -> triggers its resume ladder.
  * delta.tool_calls accumulate by string concatenation per `index` (:296-314)
    -> the index must be stable, the id only has to appear once.
  * the only usage field it reads: usage.total_tokens (:1392-1396).
  * Qoder/OpenAI usage often arrives in a `choices: []` frame AFTER the finish
    frame; if dropped, the client SDK never sees it. Hence the trailer: the
    finish chunk and usage are merged before [DONE] (pattern from
    open-sse/shared/qoder/sse.js:105-208).
  * a `data: {... "error": {...}}` frame makes openai-python raise APIError
    before [DONE] (documented in open-sse/utils/streamHelpers.js:130-133).
    We use that mechanism and do NOT fake a finish_reason on failure.
"""
from __future__ import annotations

import json
import time
from typing import Any

SSE_DONE = "data: [DONE]\n\n"

_FINISH_MAP = {
    "stop": "stop",
    "end_turn": "stop",
    "completed": "stop",
    "length": "length",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "function_call": "tool_calls",
    "tool_calls": "tool_calls",
    "content_filter": "content_filter",
    "refusal": "content_filter",
    "timeout": "error",
}


def normalize_finish_reason(value: str | None) -> str | None:
    if value is None:
        return None
    return _FINISH_MAP.get(value, value)


def chunk(*, rid: str, model: str, created: int, delta: dict[str, Any],
          finish_reason: str | None = None, usage: dict[str, Any] | None = None,
          index: int = 0) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{rid}",
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": index, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return payload


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


def error_frame(message: str, *, code: str, status: int) -> str:
    """
    Terminal frame for a stream that failed after the 200 was sent.
    """
    return sse({"error": {"message": message[:900], "type": "server_error", "code": code, "status": status}}) + SSE_DONE


def aggregate_stream_text(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Collect content from a list of chunks - used by the non-stream path and by tests.
    """
    content: list[str] = []
    reasoning: list[str] = []
    tools: dict[int, dict[str, Any]] = {}
    finish: str | None = None
    model: str | None = None
    created = int(time.time())
    for item in chunks:
        model = item.get("model") or model
        created = item.get("created") or created
        for choice in item.get("choices") or []:
            if choice.get("finish_reason"):
                finish = normalize_finish_reason(choice["finish_reason"]) or finish
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(str(delta["content"]))
            reasoning_text = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_text:
                reasoning.append(str(reasoning_text))
            for call in delta.get("tool_calls") or []:
                slot = tools.setdefault(int(call.get("index") or 0), {"id": None, "type": "function",
                                                                      "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                slot["function"]["name"] += fn.get("name") or ""
                slot["function"]["arguments"] += fn.get("arguments") or ""
    message: dict[str, Any] = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tools:
        message["tool_calls"] = [tools[key] for key in sorted(tools)]
    return {
        "content": message["content"],
        "reasoning": "".join(reasoning),
        "message": message,
        "finish_reason": finish,
        "model": model,
        "created": created,
    }


def to_completion(rid: str, model: str, chunks: list[dict[str, Any]], usage: dict[str, Any] | None) -> dict[str, Any]:
    """
    Wrap chunks into a chat.completion object (non-stream path).
    """
    parts = aggregate_stream_text(chunks)
    return {
        "id": f"chatcmpl-{rid}",
        "object": "chat.completion",
        "created": parts["created"],
        "model": parts["model"] or model,
        "choices": [{
            "index": 0,
            "message": parts["message"],
            "finish_reason": parts["finish_reason"] or "stop",
        }],
        "usage": usage or {},
    }


def usage_trailer(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in reversed(chunks):
        if isinstance(item.get("usage"), dict):
            return item["usage"]
    return None
