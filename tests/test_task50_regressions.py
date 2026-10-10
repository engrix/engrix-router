"""TASK-50 regression tests (router): usage fold Anthropic (#1), TTFT
tool-call-only (#3). Bug #4 (mark_probe) is behavioral in admin_connections --
covered by contract tests there; zcode tool_choice (#6) lives in the
providers repo's own suite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engrix_router.subscribers import usage as usage_mod  # noqa: E402


def test_extract_anthropic_usage_folds_cache_into_prompt():
    """input_tokens (Anthropic) does NOT include cache_read -- canonical prompt must."""
    chunk = {"usage": {"input_tokens": 300, "output_tokens": 50,
                       "cache_read_input_tokens": 20000,
                       "cache_creation_input_tokens": 7}}
    got = usage_mod.extract(chunk)
    assert got is not None
    assert got["prompt"] == 300 + 20000 + 7, got
    assert got["cached"] == 20000
    assert got["cache_creation"] == 7
    canon = usage_mod.canonicalize(got)
    # guard min(cached, prompt) tidak lagi memotong angka cache Anthropic
    assert canon["cached"] == 20000
    assert canon["prompt"] == 20307


def test_extract_openai_usage_untouched():
    chunk = {"usage": {"prompt_tokens": 500, "completion_tokens": 20,
                       "prompt_tokens_details": {"cached_tokens": 400}}}
    got = usage_mod.extract(chunk)
    assert got["prompt"] == 500
    assert got["cached"] == 400
    canon = usage_mod.canonicalize(got)
    assert canon["prompt"] == 500
    assert canon["cached"] == 400


def test_delta_tool_call_present():
    from engrix_router.pipeline import runner
    frame = {"choices": [{"index": 0,
                          "delta": {"tool_calls": [{"index": 0, "id": "x",
                                                    "function": {"name": "f", "arguments": ""}}]}}]}
    assert runner._delta_tool_call_present(frame) is True
    assert runner._delta_tool_call_present({"choices": [{"delta": {"content": "hi"}}]}) is False
    assert runner._delta_tool_call_present({}) is False
