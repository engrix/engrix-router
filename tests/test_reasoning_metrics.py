"""Reasoning accounting + honest TTFT, end-to-end through the real runner/DB.

Two regressions these lock out: (1) a streamed thinking delta used to leave
requests.reasoning at 0 when the vendor's usage block omits reasoning_tokens
(the real ZCode start-plan shape), and (2) TTFT was stamped on the first raw
SSE frame -- a role/ping delta the client never sees -- measuring handshake
instead of first visible token. Same faking style as test_services_and_hooks:
patch the transport method, drive the pipeline, read the trace row back.
"""
from __future__ import annotations

import asyncio
import json

from engrix_router.accounts import connections
from engrix_router.pipeline import runner
from engrix_router.providers import openai as openai_mod
from engrix_router.providers import registry
from engrix_router.subscribers import trace as trace_mod


def _sse_frames(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        payload = line.strip()
        if payload.startswith("data:") and "DONE" not in payload:
            out.append(json.loads(payload[5:].strip()))
    return out


def test_streamed_reasoning_lands_in_db_and_ttft_skips_empty_frames(monkeypatch):
    long_thinking = "x" * 200  # 200 chars -> 50 tokens on the chars/4 estimate
    frames = [
        # # frame-0 delta KOSONG konten/reasoning (role ping) — dulu ini yang
        # # nge-set TTFT, padahal klien belum dikasih apa-apa buat dilihat.
        {"choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"choices": [{"index": 0, "delta": {"reasoning_content": long_thinking}}]},
        {"choices": [{"index": 0, "delta": {"content": "Hello"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         # # usage TANPA reasoning_tokens: bentuk ZCode start-plan sungguhan.
         "usage": {"prompt_tokens": 1000, "completion_tokens": 80,
                   "total_tokens": 1080}},
    ]

    async def fake_open_stream(self, request):
        for frame in frames:
            yield frame

    monkeypatch.setattr(openai_mod.OpenAICompatibleProvider, "open_stream", fake_open_stream)
    conn = connections.create(provider="openai", name="r41", api_key="sk-r41")

    async def drive():
        return [chunk async for chunk in runner.stream_chat(
            {"model": "openai/gpt-test", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]})]

    lines = asyncio.run(drive())
    parsed = _sse_frames(lines)
    assert len(parsed) == 5  # 4 frame vendor + 1 trailer usage yang ditambahin pipeline
    request_id = str(parsed[-1].get("id") or "").removeprefix("chatcmpl-")
    row = trace_mod.get(request_id)
    assert row["status"] == trace_mod.STATUS_OK
    assert row["reasoning"] == 50, (row["reasoning"], "chars/4 estimate of the 200-char delta")
    assert row["completion"] == 80 and row["prompt"] == 1000  # vendor usage wins
    # # ttft bukan 0-nya frame kosong: harus ada dan masuk akal di dalam total
    assert row["ttft_ms"] is not None and 0 < row["ttft_ms"] <= row["total_ms"]
    connections.delete(conn["id"])


def test_stream_with_only_empty_deltas_never_stamps_ttft(monkeypatch):
    async def fake_open_stream(self, request):
        yield {"choices": [{"index": 0, "delta": {"role": "assistant"}}]}
        yield {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5}}

    monkeypatch.setattr(openai_mod.OpenAICompatibleProvider, "open_stream", fake_open_stream)
    conn = connections.create(provider="openai", name="r41e", api_key="sk-r41e")

    async def drive():
        return [chunk async for chunk in runner.stream_chat(
            {"model": "openai/gpt-test", "stream": True,
             "messages": [{"role": "user", "content": "hi"}]})]

    parsed = _sse_frames(asyncio.run(drive()))
    row = trace_mod.get(str(parsed[-1]["id"]).removeprefix("chatcmpl-"))
    # # klien nggak pernah lihat token → TTFT tetap None, jangan ngaku 0ms-kinclong
    assert row["ttft_ms"] is None, row["ttft_ms"]
    assert row["reasoning"] == 0
    connections.delete(conn["id"])


def test_nonstream_reasoning_message_is_estimated(monkeypatch):
    async def fake_complete(self, request):
        return {"id": "c", "object": "chat.completion",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": "OK",
                    "reasoning_content": "y" * 400}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 1,
                          "total_tokens": 11}}

    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete", fake_complete)
    conn = connections.create(provider="openai", name="r41ns", api_key="sk-r41ns")
    result = asyncio.run(runner.run_chat({"model": "openai/gpt-test",
                                          "messages": [{"role": "user", "content": "hi"}]}))
    row = trace_mod.get(result["id"].removeprefix("chatcmpl-"))
    assert row["reasoning"] == 100, row["reasoning"]
    connections.delete(conn["id"])
