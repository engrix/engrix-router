"""Token introspection shared by every layer that persists credentials.

A pasted OAuth blob carries more than the string itself: a JWT names its subject,
its issue and expiry instants, the scopes and the client that obtained it. The
gateway used to keep only the raw string, so ``expires_at`` stayed NULL on every
OAuth connection and the router could not tell a live token from a dead one until
the vendor refused a request. This module is the one place that reads a token and
says what it already knows about itself.

Nothing here verifies a signature. The token is what authenticates; we only ever
read our own copy of it, so a cryptographic check would add nothing. Decoding is
therefore best-effort -- a value that is not a JWT yields no claims and the caller
keeps whatever it already had. That also means an opaque API key passes through
untouched instead of being rejected.

Layer: ``core`` (rank 0). Standard library only -- no vendor knowledge, because a
claim name that only one vendor emits does not belong in the foundation.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Any

# Claim yang boleh dibaca dashboard: metadata token, bukan materi rahasia.
# `sub`/`aud`/`scope` menjawab "akun dan izin apa ini", bukan "token-nya apa".
PUBLIC_CLAIMS: tuple[str, ...] = (
    "sub",
    "iss",
    "aud",
    "azp",
    "scope",
    "typ",
    "jti",
    "iat",
    "exp",
    "nbf",
    "auth_time",
    "uin",
    "tenant",
)

# Pemisah bentuk komposit yang dipakai klien desktop: "<account-id>+<jwt>".
# base64url tidak pernah menghasilkan "+", jadi karakter ini aman jadi penanda.
_COMPOSITE_SEPARATOR = "+"


def looks_like_jwt(value: str | None) -> bool:
    """True when ``value`` has the three dot-separated segments a JWT needs."""
    return bool(value) and str(value).count(".") == 2


def split_composite(value: str | None) -> tuple[str | None, str | None]:
    """Separate the ``<account-id>+<jwt>`` form the desktop client stores.

    Electron's safeStorage keeps the account id glued to the token. Sending that
    whole string as a bearer credential is refused by the vendor, so the pair is
    split at ingest: element 0 is the account id (None when absent), element 1 is
    the token to store. A value without the separator comes back whole, which is
    the normal case for an API key.
    """
    if not value:
        return None, None
    text = str(value)
    if _COMPOSITE_SEPARATOR not in text:
        return None, text
    head, tail = text.split(_COMPOSITE_SEPARATOR, 1)
    if not looks_like_jwt(tail):
        return None, text
    return head.strip() or None, tail.strip()


def decode_claims(token: str | None) -> dict[str, Any]:
    """Payload of a JWT, unverified. Empty dict when the value is not one.

    Never raises: a malformed segment, bad padding or non-object JSON all end as
    "no claims", because a credential that fails to introspect must still be
    storable.
    """
    if not looks_like_jwt(token):
        return {}
    payload = str(token).split(".")[1]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        decoded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def subject(claims: dict[str, Any] | None) -> str | None:
    """The ``sub`` claim as a plain string, or None when it is absent."""
    value = (claims or {}).get("sub")
    return str(value) if value else None


def epoch_to_iso(value: Any) -> str | None:
    """ISO-8601 UTC stamp for an epoch-seconds claim.

    Milliseconds are accepted too: a vendor that emits ``exp`` in ms would
    otherwise be read as a date in the year 58000, and every token would look
    valid forever.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1e11:
        number /= 1000.0
    try:
        moment = datetime.fromtimestamp(number, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def expiry_iso(claims: dict[str, Any] | None) -> str | None:
    """``expires_at`` for a claim set: the ``exp`` claim as ISO-8601."""
    return epoch_to_iso((claims or {}).get("exp"))


def issued_iso(claims: dict[str, Any] | None) -> str | None:
    """When the token was minted (``iat``). Used by reports, never for routing."""
    return epoch_to_iso((claims or {}).get("iat"))


def normalise(token: str | None) -> tuple[str | None, dict[str, Any]]:
    """Return the token to store plus the claims it carries.

    The composite prefix is dropped here so no write path has to know about it,
    and the claims are decoded from the token that actually gets sent upstream.
    """
    _, bare = split_composite(token)
    return bare, decode_claims(bare)


def public_summary(claims: dict[str, Any] | None) -> dict[str, Any]:
    """Only the claims a dashboard may read, with the token material excluded."""
    source = claims or {}
    return {key: source[key] for key in PUBLIC_CLAIMS if key in source}
