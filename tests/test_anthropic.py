"""Anthropic format tests: translation both directions, and /v1/messages over the pipeline.

Two claims get proven here (they are the reason ADR-0001 exists):
  1. the gateway really is a translator -- inbound Anthropic shape leaves as Anthropic
     shape after passing through the SAME pipeline that serves /v1/chat/completions;
  2. the two directions of one format cannot drift: from_canonical_completion() and
     completion_from_message() are tested against each other.

No network: provider I/O is stubbed, event translation is a pure function.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from engrix_router.core.formats import anthropic as fmt
from engrix_router.identity import api_keys
from engrix_router.providers import anthropic as anthropic_provider
from engrix_router.providers import registry
from engrix_router.web import server

TEXT_FRAME = {"type": "content_block_delta", "index": 0,
              "delta": {"type": "text_delta", "text": "HI"}}


# ── 1. inbound request -> canonical ──────────────────────────────────────────
def test_system_is_hoisted_and_unknown_fields_pass_through():
    canonical = fmt.to_canonical_body({
        "model": "claude-sonnet-4-5", "max_tokens": 64, "temperature": 0.2,
        "system": "be terse", "foo_knob": {"vendor": "value"},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert canonical["messages"][0] == {"role": "system", "content": "be terse"}
    assert canonical["messages"][1] == {"role": "user", "content": "hi"}
    assert canonical["max_tokens"] == 64 and canonical["temperature"] == 0.2
    # field yang gak kita kenal JANGAN dibuang: klien harus bisa ngatur knob vendor
    assert canonical["foo_knob"] == {"vendor": "value"}


def test_tool_result_block_becomes_a_tool_message():
    canonical = fmt.to_canonical_body({"model": "m", "messages": [
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1",
                                      "content": "42"}]}]})
    assert {"role": "tool", "tool_call_id": "call_1", "content": "42"} in canonical["messages"]


def test_tools_and_tool_choice_are_translated():
    canonical = fmt.to_canonical_body({
        "model": "m", "messages": [], "tools": [{"name": "add", "description": "sum",
                                                 "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "tool", "tool_name": "add"},
        "stop_sequences": ["END"]})
    assert canonical["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert canonical["tool_choice"] == {"type": "function", "function": {"name": "add"}}
    assert canonical["stop"] == ["END"]


# ── 2. canonical -> inbound response shape ───────────────────────────────────
def test_completion_becomes_anthropic_message():
    message = fmt.from_canonical_completion("abc", "anthropic/claude-x", {
        "model": "claude-x",
        "choices": [{"message": {"role": "assistant", "content": "hello",
                                 "reasoning_content": "thinking..."},
                     "finish_reason": "tool_calls"}],
        "usage": {"prompt": 11, "completion": 7, "cached": 3, "cache_creation": 0}})
    assert message["type"] == "message" and message["id"] == "msg_abc"
    assert message["stop_reason"] == "tool_use"
    assert {"type": "thinking", "thinking": "thinking..."} in message["content"]
    assert {"type": "text", "text": "hello"} in message["content"]
    assert message["usage"] == {"input_tokens": 11, "output_tokens": 7,
                               "cache_read_input_tokens": 3}


def test_the_two_directions_agree():
    """from_canonical -> completion_from must land back on the same content (ADR-0001)."""
    completion = {
        "id": "chatcmpl-x", "model": "claude-x",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": "the answer",
                                 "tool_calls": [{"id": "t1", "type": "function",
                                                 "function": {"name": "add",
                                                              "arguments": "{\"a\":1}"}}]}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    back = fmt.completion_from_message(fmt.from_canonical_completion("x", "claude-x", completion))
    message = back["choices"][0]["message"]
    assert message["content"] == "the answer"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"a": 1}
    assert message["tool_calls"][0]["id"] == "t1"
    assert back["usage"]["total_tokens"] == 7


# ── 3. outbound provider: native payload and event decoding ─────────────────
def _provider():
    return registry.get_provider("anthropic")


def test_transform_request_fills_mandatory_max_tokens_and_hoists_system():
    payload = _provider().transform_request(
        {"model": "anthropic/claude-x", "max_tokens": 32,
         "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "q"}],
         "stop": ["X"]}, creds=_creds(), stream=False)
    assert payload["model"] == "claude-x"          # prefix kanal, bukan bagian vendor
    assert payload["system"] == "sys"
    assert payload["messages"] == [{"role": "user", "content": [{"type": "text", "text": "q"}]}]
    assert payload["stop_sequences"] == ["X"]


def test_transform_request_defaults_max_tokens_when_the_client_omits_it():
    payload = _provider().transform_request({"model": "anthropic/m", "messages": []},
                                            creds=_creds(), stream=False)
    assert payload["max_tokens"] == anthropic_provider._DEFAULT_MAX_TOKENS


def _creds():
    from engrix_router.core.types import Credentials

    return Credentials(connection_id="c1", provider="anthropic", auth_type="apikey",
                       name="test", token="sk-test")


def _unwrap(event: dict) -> dict | None:
    return _provider().unwrap_data(json.dumps(event))


def test_text_and_thinking_deltas_stay_separate():
    assert _unwrap(TEXT_FRAME)["choices"][0]["delta"]["content"] == "HI"
    thinking = _unwrap({"type": "content_block_delta", "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "hmm"}})
    assert thinking["choices"][0]["delta"]["reasoning_content"] == "hmm"


def test_tool_events_map_onto_indexed_tool_calls():
    start = _unwrap({"type": "content_block_start", "index": 2,
                     "content_block": {"type": "tool_use", "id": "tu_1", "name": "add"}})
    fragment = _unwrap({"type": "content_block_delta", "index": 2,
                        "delta": {"type": "input_json_delta", "partial_json": "{\"a\""}})
    call = start["choices"][0]["delta"]["tool_calls"][0]
    assert call["id"] == "tu_1" and call["function"]["name"] == "add"
    assert call["index"] == fragment["choices"][0]["delta"]["tool_calls"][0]["index"] == 2
    assert fragment["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == "{\"a\""


def test_message_lifecycle_events_map_to_finish_and_usage():
    start = _unwrap({"type": "message_start",
                     "message": {"id": "msg_1", "model": "claude-x",
                                 "usage": {"input_tokens": 9}}})
    assert start["usage"]["input_tokens"] == 9
    delta = _unwrap({"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
                     "usage": {"output_tokens": 4}})
    assert delta["choices"][0]["finish_reason"] == "length"
    assert delta["usage"]["output_tokens"] == 4
    assert _unwrap({"type": "message_stop"}) is None
    assert _unwrap({"type": "ping"}) is None


def test_error_event_is_not_silently_dropped():
    """base._open_stream_inner turns a frame with `error` into UpstreamError; that needs the dict back."""
    event = {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}}
    assert _provider().unwrap_data(json.dumps(event)) == event


# ── 4. /v1/messages through the gateway ──────────────────────────────────────
@pytest.fixture
def client_key():
    created = api_keys.create_key("anthropic-test")
    return {"authorization": f"Bearer {created['key']}"}


@pytest.fixture
def connection():
    """Routing needs a candidate: without a connection /v1/messages answers 503 (by design)."""
    from engrix_router.accounts import connections

    return connections.create(provider="anthropic", name="test-acct", api_key="sk-test")


def _frames():
    from engrix_router.core.formats import openai as openai_fmt

    return [
        openai_fmt.chunk(rid="r", model="anthropic/claude-x", created=1,
                         delta={"role": "assistant", "content": "HEL"}),
        openai_fmt.chunk(rid="r", model="anthropic/claude-x", created=1, delta={"content": "LO"}),
        openai_fmt.chunk(rid="r", model="anthropic/claude-x", created=1, delta={},
                         finish_reason="stop",
                         usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}),
    ]


def test_messages_nonstream_returns_anthropic_shape(monkeypatch, client_key, connection):
    async def fake_complete(self, request):
        return {"id": "chatcmpl-1", "model": "claude-x",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "HELLO"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}}

    monkeypatch.setattr(anthropic_provider.AnthropicProvider, "complete", fake_complete)
    with TestClient(server.app) as client:
        response = client.post("/v1/messages", headers=client_key, json={
            "model": "claude-x", "max_tokens": 16,
            "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "HELLO"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"] == {"input_tokens": 4, "output_tokens": 2}


def test_messages_stream_emits_the_anthropic_event_sequence(monkeypatch, client_key, connection):
    async def fake_stream(self, request):
        for frame in _frames():
            yield frame

    monkeypatch.setattr(anthropic_provider.AnthropicProvider, "open_stream", fake_stream)
    with TestClient(server.app) as client:
        with client.stream("POST", "/v1/messages", json={
                "model": "claude-x", "max_tokens": 16, "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}, headers=client_key) as response:
            raw = "".join(response.iter_text())
    events = [line[6:].strip() for line in raw.split("\n") if line.startswith("event:")]
    assert events[0] == "message_start"
    assert events[-1] == "message_stop"
    assert "content_block_start" in events and "content_block_delta" in events
    assert "content_block_stop" in events and "message_delta" in events
    text = "".join(json.loads(line[5:])["delta"]["text"] for line in raw.split("\n")
                   if line.startswith("data:") and "text_delta" in line)
    assert text == "HELLO"
    assert "[DONE]" not in raw            # OpenAI sentinel, bukan bagian kontrak Anthropic
    stop = [json.loads(line[5:]) for line in raw.split("\n")
            if line.startswith("data:") and '"message_delta"' in line]
    assert stop[0]["delta"]["stop_reason"] == "end_turn"


def test_messages_rejects_an_unknown_provider_with_anthropic_error_shape(client_key):
    with TestClient(server.app) as client:
        response = client.post("/v1/messages", headers=client_key, json={
            "model": "nosuchvendor/model", "max_tokens": 8,
            "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
