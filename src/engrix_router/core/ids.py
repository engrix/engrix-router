"""
Time-sortable IDs (ULID shape) without any external dependency.

Why not uuid4: request_id orders logs and traces and matches rows across
tables. A random uuid4 scatters the PRIMARY KEY and id order stops
correlating with time order.

Layout: 48 bits of epoch-ms + 80 bits of random, rendered as 26 Crockford
base32 characters - the same length as a standard ULID, so `sorted()` works
without parsing a time column.
"""
from __future__ import annotations

import secrets
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (tanpa I L O U)
_TIME_LEN = 12
_RANDOM_LEN = 14
ID_LENGTH = _TIME_LEN + _RANDOM_LEN  # 26


def _encode(value: int, length: int) -> str:
    chars = ["0"] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_id(now_ms: int | None = None) -> str:
    """
    26 characters; a newer ID sorts larger lexicographically.
    """
    ts = int(time.time() * 1000) if now_ms is None else int(now_ms)
    return _encode(ts & ((1 << 48) - 1), _TIME_LEN) + _encode(
        secrets.randbits(_RANDOM_LEN * 5), _RANDOM_LEN
    )


def time_ms_of(candidate: str) -> int | None:
    """
    Epoch-ms of an ID, or None when it is not one of ours.
    """
    if len(candidate) != ID_LENGTH:
        return None
    upper = candidate.upper()
    total = 0
    for char in upper[:_TIME_LEN]:
        index = _ALPHABET.find(char)
        if index < 0:
            return None
        total = (total << 5) | index
    return total


def is_valid_id(candidate: str) -> bool:
    return time_ms_of(candidate) is not None
