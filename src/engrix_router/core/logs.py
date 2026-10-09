"""
Console logging (Engrix style) + in-memory ring buffer + SSE fan-out.

Line format follows 9router (open-sse/handlers/chatCore.js:251 and
handlers/chatCore/requestDetail.js:85-101) because the owner already reads
that shape - with one structural fix: every line can be tied to a request
through `rid=`. 9router has no correlation at all; its tags are 8 emojis of a
session hash (chatCore.js:67-74).

Levels DEBUG0 INFO1 WARN2 ERROR3, default INFO. Errors bypass level gating
(pattern from open-sse/utils/logger.js:41-44): a provider failure must never
go silent because the operator set the level to warn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

from engrix_router.core import config

LOGGER_NAME = "engrix.router"
NS_PROVIDER = "PROVIDER"
NS_ROUTE = "ROUTE"
NS_HEALTH = "HEALTH"
NS_KEYS = "KEYS"
NS_QUOTA = "QUOTA"
NS_PROXY = "PROXY"
NS_TRACE = "TRACE"
NS_BUDGET = "BUDGET"
NS_APP = "APP"

_LEVEL_COLORS = {
    "DEBUG": "\x1b[37m",
    "INFO": "\x1b[36m",
    "WARN": "\x1b[33m",
    "ERROR": "\x1b[31m",
}
_RESET = "\x1b[0m"

_ring: deque[dict[str, Any]] = deque(maxlen=max(50, config.LOG_RING_SIZE))
_sinks: set[asyncio.Queue] = set()
_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
_configured = False


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = datetime.fromtimestamp(record.created, timezone.utc).strftime("%H:%M:%S")
        ns = getattr(record, "ns", "")
        rid = getattr(record, "rid", "")
        prefix = f"[{stamp}]"
        if ns:
            prefix += f" {ns}"
        if rid:
            prefix += f" · rid={rid}"
        body = record.getMessage()
        line = f"{prefix} {body}"
        if _tty and record.levelno >= logging.WARNING:
            color = _LEVEL_COLORS.get("WARN" if record.levelno < logging.ERROR else "ERROR", "")
            line = f"{color}{line}{_RESET}"
        return line


def configure(level: str | None = None) -> logging.Logger:
    """
    Idempotent. Called at startup and by tests.
    """
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if _configured:
        return logger
    # Windows console default cp1252: baris ber-emoji (📊/✗/▶) bikin
    # UnicodeEncodeError di dalam StreamHandler dan logging nelen error-nya
    # jadi "--- Logging error ---" palsu. Paksa UTF-8 dengan replace.
    stream = sys.stdout
    try:
        if (stream.encoding or "").lower().replace("-", "") not in {"utf8"}:
            stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    numeric = getattr(logging, str(level or config.LOG_LEVEL).upper(), logging.INFO)
    logger.setLevel(numeric)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter())
    logger.addHandler(handler)
    if config.LOG_FILE_ENABLED:
        # Rotating, karena dashboard dan `tail` sama-sama gak berguna kalau file log
        # tumbuh tanpa batas (9router: dump 7 file/request TANPA rotasi,
        # utils/requestLogger.js:17 -- persis yang gak kita ulang).
        from logging.handlers import RotatingFileHandler

        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            config.LOG_DIR / f"{config.APP_NAME}.log",
            maxBytes=config.LOG_FILE_MAX_BYTES, backupCount=config.LOG_FILE_BACKUPS,
            encoding="utf-8", errors="replace")
        file_handler.setFormatter(_Formatter())
        logger.addHandler(file_handler)
    _configured = True
    return logger


def get() -> logging.Logger:
    configure()
    return logging.getLogger(LOGGER_NAME)


def _emit(level: int, ns: str, message: str, rid: str | None) -> None:
    logger = configure()
    extra = {"ns": ns, "rid": rid or ""}
    logger.log(level, message, extra=extra)
    record = logging.LogRecord(LOGGER_NAME, level, __file__, 0, message, None, None)
    record.__dict__.update(extra)
    entry = {
        "ts": int(time.time() * 1000),
        "level": logging.getLevelName(level).lower(),
        "ns": ns,
        "rid": rid or "",
        "line": _Formatter().format(record),
    }
    _ring.append(entry)
    for queue in list(_sinks):
        try:
            queue.put_nowait(entry)
        except asyncio.QueueFull:
            pass  # konsumen SSE lambat: buang, jangan bikin request gagal


def debug(ns: str, message: str, *, rid: str | None = None) -> None:
    _emit(logging.DEBUG, ns, message, rid)


def info(ns: str, message: str, *, rid: str | None = None) -> None:
    _emit(logging.INFO, ns, message, rid)


def warn(ns: str, message: str, *, rid: str | None = None) -> None:
    _emit(logging.WARNING, ns, message, rid)


def error(ns: str, message: str, *, rid: str | None = None) -> None:
    _emit(logging.ERROR, ns, message, rid)


# ── grammar baris khusus gateway ─────────────────────────────────────────────
def request_start(*, rid: str, method: str, model: str, provider: str, stream: bool,
                  messages: int, tools: int, tier: str | None, account: str, lane: str) -> None:
    parts = [
        f"▶ {method} {model} → {provider}",
        f"{'STREAM' if stream else 'NON-STREAM'}",
        f"{messages} MSG",
    ]
    if tools:
        parts.append(f"{tools} TOOL")
    if tier:
        parts.append(f"TIER {tier}")
    parts.append(f"ACC={account}")
    parts.append(f"LANE={lane}")
    info(NS_ROUTE, " · ".join(parts), rid=rid)


def request_done(*, rid: str, total_ms: int, ttft_ms: int | None, prompt: int, cached: int,
                 cache_creation: int, completion: int, reasoning: int, cost_usd: float,
                 frames: int, status: str) -> None:
    in_str = f"IN {prompt}"
    cache_bits = []
    if cached:
        cache_bits.append(f"↻{cached}")
    if cache_creation:
        cache_bits.append(f"+{cache_creation}")
    if cache_bits:
        in_str += f" (CACHE {' '.join(cache_bits)})"
    tail = f"OUT {completion}"
    if reasoning:
        tail += f" (RTHINK {reasoning})"
    info(
        NS_ROUTE,
        f"📊 {status.upper()} {total_ms}ms · TTFT {ttft_ms or 0}ms · {in_str} · {tail} "
        f"· {frames}F · ${cost_usd:.6f}",
        rid=rid,
    )


def request_failed(*, rid: str, provider: str, model: str, total_ms: int, error_class: str,
                   http_out: int, vendor_code: str | None, account: str, message: str,
                   lock_ms: int | None, retry_after_s: int | None) -> None:
    bits = [f"✗ ERROR {http_out} class={error_class}"]
    if vendor_code:
        bits.append(f"code={vendor_code}")
    bits.append(f"{provider}/{model}")
    bits.append(f"ACC={account}")
    bits.append(f"{total_ms}ms")
    if lock_ms:
        bits.append(f"locked {lock_ms // 1000}s")
    if retry_after_s:
        bits.append(f"retry-after {retry_after_s}s")
    line = " · ".join(bits) + f"\n    {message[:400]}"
    error(NS_HEALTH, line, rid=rid)


# ── redaksi ──────────────────────────────────────────────────────────────────
def redact_headers(headers: dict[str, Any] | None) -> dict[str, str]:
    """
    Must run before any header reaches a log or a trace.

    The equivalent 9router function returns the headers untouched (its
    masking is commented out, open-sse/utils/requestLogger.js:72-91).
    """
    if not headers:
        return {}
    blocked = set(config.REDACT_HEADERS)
    out: dict[str, str] = {}
    for key, value in headers.items():
        low = str(key).lower()
        if low in blocked or "token" in low or "secret" in low:
            out[str(key)] = f"[redacted {len(str(value))} chars]"
        else:
            out[str(key)] = str(value)
    return out


# ── konsumsi (dashboard + /api/logs) ─────────────────────────────────────────
def tail(limit: int = 200) -> list[dict[str, Any]]:
    items = list(_ring)[-max(1, min(limit, config.LOG_RING_SIZE)):]
    return items


def clear() -> None:
    _ring.clear()


def subscribe() -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    _sinks.add(queue)
    return queue


def unsubscribe(queue: asyncio.Queue) -> None:
    _sinks.discard(queue)


def as_json(entry: dict[str, Any]) -> str:
    return json.dumps(entry, ensure_ascii=False, default=str)
