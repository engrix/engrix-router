"""The shared provider contract, run against the built-in OpenAI-compatible provider.

If this suite passes, the gateway can route a vendor that behaves like OpenAI without
writing a line of vendor code -- which is the baseline every adapter is measured against.
"""
from __future__ import annotations

import json

from engrix_router.testing.contract import ProviderContract

_STREAM = [
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-test",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "HEL"}, "finish_reason": None}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-test",
     "choices": [{"index": 0, "delta": {"content": "LO"}, "finish_reason": None}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-test",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": "gpt-test",
     "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}},
]


class TestOpenAICompatibleContract(ProviderContract):
    provider_id = "openai"
    model = "openai/gpt-test"
    expected_content = "HELLO"
    expected_usage_total = 18
    probe_tier = "models_list"
    chat_body = json.dumps({
        "id": "c", "object": "chat.completion", "created": 1, "model": "gpt-test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "HELLO"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
    stream_text = "".join(f"data: {json.dumps(frame)}\n\n" for frame in _STREAM) + "data: [DONE]\n\n"
    models_body = json.dumps({"object": "list",
                              "data": [{"id": "gpt-test", "name": "gpt-test", "object": "model"}]})
    error_body = json.dumps({"error": {"message": "rate limited", "type": "rate_limit_error"}})
