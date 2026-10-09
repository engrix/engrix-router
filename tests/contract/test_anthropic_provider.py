"""Contract suite for the Anthropic-compatible provider: a non-OpenAI wire shape.

The canned bodies here are Anthropic's own (typed events, `input_tokens`,
`stop_reason`), so passing the same suite proves the translation layer, not a stub
that was written to look like OpenAI.
"""
from __future__ import annotations

import json

from engrix_router.testing.contract import ProviderContract

_EVENTS = [
    {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant",
                                          "model": "claude-test", "content": [],
                                          "usage": {"input_tokens": 11, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0,
     "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "HEL"}},
    {"type": "content_block_delta", "index": 0,
     "delta": {"type": "text_delta", "text": "LO"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
     "usage": {"output_tokens": 7}},
    {"type": "message_stop"},
]


_EVENTS_TOOL = [
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "tu_1", "name": "add"}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": '{"a":'}},
    {"type": "content_block_delta", "index": 1,
     "delta": {"type": "input_json_delta", "partial_json": '1}'}},
    {"type": "content_block_stop", "index": 1},
]


class TestAnthropicContract(ProviderContract):
    provider_id = "anthropic"
    model = "anthropic/claude-test"
    expected_content = "HELLO"
    expected_usage_total = 18            # 11 input + 7 output, from two separate events
    chat_body = json.dumps({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-test",
        "content": [{"type": "text", "text": "HELLO"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 7}})
    stream_text = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in _EVENTS)
    tool_stream = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in _EVENTS_TOOL)
    models_body = json.dumps({"data": [{"id": "claude-test", "display_name": "Claude Test"}]})
    error_body = json.dumps({"type": "error",
                             "error": {"type": "rate_limit_error", "message": "slow down"}})
