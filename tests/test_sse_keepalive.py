"""SSE keepalive: silent upstream, honest transport.

Two behaviors are pinned here because both were changed for the "stream looks
dead while the model thinks" report:

  * a gap longer than the interval yields an SSE comment and NOTHING else -- no
    fabricated data frame, no fake finish_reason;
  * frames themselves are forwarded byte-for-byte, in order, with no added delay
    when the upstream is chatty.

Plus one honesty rule on errors: the vendor's sentence stays in `message`, and
whatever the gateway decided for itself lives in `engrix_note`.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from engrix_router.api import sse
from engrix_router.core.formats import openai as openai_fmt


async def _slow_frames(gaps: list[float], frames: list[str]):
    for gap, frame in zip(gaps, frames):
        await asyncio.sleep(gap)
        yield frame


@pytest.mark.asyncio
async def test_keepalive_only_during_silence_and_frames_untouched():
    frames = ["data: 1\n\n", "data: 2\n\n", "data: [DONE]\n\n"]
    # jeda 0.25s dengan interval 0.05s -> harus muncul komentar di antara frame
    received = [chunk async for chunk in sse.with_keepalive(
        _slow_frames([0.25, 0.25, 0.0], frames), interval_s=0.05)]

    assert [chunk for chunk in received if chunk.startswith("data:")] == frames
    keepalives = [chunk for chunk in received if chunk == sse.KEEPALIVE_COMMENT]
    assert keepalives, "silence longer than the interval must produce a keepalive"
    assert sse.KEEPALIVE_COMMENT.startswith(": "), "keepalive must be an SSE comment"


@pytest.mark.asyncio
async def test_no_keepalive_when_upstream_is_not_silent():
    frames = [f"data: {i}\n\n" for i in range(5)]
    received = [chunk async for chunk in sse.with_keepalive(
        _slow_frames([0.0] * len(frames), frames), interval_s=0.5)]
    assert received == frames  # nol komentar, urutan utuh


@pytest.mark.asyncio
async def test_interval_zero_is_a_pure_passthrough():
    frames = ["data: a\n\n", "data: [DONE]\n\n"]
    received = [chunk async for chunk in sse.with_keepalive(
        _slow_frames([0.02, 0.0], frames), interval_s=0)]
    assert received == frames


@pytest.mark.asyncio
async def test_slow_upstream_stream_is_not_cancelled_by_the_keepalive_timer():
    """The timer must never turn a slow model into a truncated answer."""

    async def very_slow():
        yield "data: start\n\n"
        await asyncio.sleep(0.2)
        yield "data: end\n\n"

    received = [chunk async for chunk in sse.with_keepalive(very_slow(), interval_s=0.05)]
    assert "data: end\n\n" in received


def test_error_frame_keeps_the_vendor_sentence_verbatim():
    vendor = '{"code":1005,"msg":"exceed quota limit"}'
    frame = openai_fmt.error_frame(vendor, code="quota_daily", status=429,
                                   note="attempt cap of 1 reached")
    # frame pertama = `data: {json}`, frame terakhir = sentinel [DONE]
    payload = json.loads(frame.splitlines()[0].removeprefix("data: "))
    assert payload["error"]["message"] == vendor, "pesan vendor harus utuh"
    assert payload["error"]["engrix_note"] == "attempt cap of 1 reached"
    assert payload["error"]["code"] == "quota_daily"
