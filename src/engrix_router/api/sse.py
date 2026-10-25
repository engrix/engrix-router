"""SSE keepalive: a comment line every N seconds while the upstream is silent.

Why this exists (measured, not guessed):

An upstream model can sit on a long reasoning phase. During that window the gateway
has nothing to forward, so the socket is silent -- the client sees a stream that
started fast and then "hangs", and any proxy in between (nginx, a tunnel, a laptop
sleeping on an idle socket) is free to drop or re-buffer it. 9router learned the same
lesson on its log stream (app/api/translator/console-logs/stream/route.js:26-32) and
keeps its listeners alive with a comment line.

The keepalive is `: comment` on purpose:
  * it is valid SSE and the SSE spec says clients ignore it;
  * it is not a `data:` frame, so it never becomes a fake token, a fake
    finish_reason, or an entry in anyone's transcript. Requests and answers stay
    exactly what the vendor sent -- this only keeps the transport warm.

Pacing is unaffected: frames are yielded the instant they arrive, and the timer only
fires when the gap between two frames exceeds the interval.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

KEEPALIVE_COMMENT = ": keepalive\n\n"


def keepalive_interval_s() -> float:
    """Seconds of silence before a keepalive comment; 0 disables it."""
    from engrix_router.storage import settings

    try:
        value = settings.get_int("stream.keepalive_s")
    except Exception:  # setting belum ada di DB lama -- pakai default
        return 15.0
    return max(0.0, float(value))


async def with_keepalive(frames: AsyncIterator[str], interval_s: float | None = None) -> AsyncIterator[str]:
    """Forward `frames` verbatim, inserting an SSE comment during long silences."""
    interval = keepalive_interval_s() if interval_s is None else max(0.0, interval_s)
    iterator = frames.__aiter__()
    if interval <= 0:
        async for frame in iterator:
            yield frame
        return
    pending: asyncio.Future | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            try:
                # shield: timeout membatalkan PENUNGGUAN kita, bukan pembacaan upstream,
                # jadi model yang lambat tidak berubah jadi stream yang dibatalkan.
                async with asyncio.timeout(interval):
                    frame = await asyncio.shield(pending)
            except TimeoutError:
                yield KEEPALIVE_COMMENT
                continue
            except StopAsyncIteration:
                return
            pending = None
            yield frame
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(BaseException):
                await aclose()
