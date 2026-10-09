"""API keys for the clients that call this gateway.

Deviation from 9router (deliberate -- this is security, not style):
  * key value = `secrets.token_urlsafe(32)`, not `Math.random()` 6 chars +
    HMAC with a hard-coded default secret ("endpoint-proxy-api-key-secret",
    src/shared/utils/apiKey.js:3-15).
  * what is stored = SHA-256 hash + a short prefix. The plaintext is emitted
    once, at create. 9router stores plaintext (apiKeysRepo.js:41-44) and
    GET /api/keys hands back every key in full (rowToKey, :4-14).
  * `usageHistory.apiKey` in 9router keeps the raw client key (usageRepo.js:287);
    our request rows only store api_key_id (FK).
"""
from __future__ import annotations

import hashlib
import secrets
from typing import Any

from engrix_router.storage.sqlite import execute, now_iso, query, query_one
from engrix_router.core import logs as applog
from engrix_router.core.ids import new_id

KEY_PREFIX_LEN = 12
_LABEL = "egk-live-"


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def create_key(name: str, *, machine_id: str | None = None) -> dict[str, Any]:
    """Returns the row + `secret`, the only moment that secret is ever known."""
    if not name or not name.strip():
        raise ValueError("name is required")
    key_id = new_id()
    secret = _LABEL + secrets.token_urlsafe(32)
    fingerprint = secret[: KEY_PREFIX_LEN + len(_LABEL)]
    execute(
        "INSERT INTO api_keys(id, name, key_hash, key_prefix, machine_id, is_active, created_at)"
        " VALUES(?,?,?,?,?,1,?)",
        (key_id, name.strip(), _hash(secret), fingerprint, machine_id, now_iso()),
    )
    applog.info(applog.NS_KEYS, f"key created: {name.strip()} ({fingerprint}…)")
    return {"id": key_id, "name": name.strip(), "key": secret, "key_prefix": fingerprint,
            "machine_id": machine_id, "created_at": now_iso(), "is_active": 1}


def verify(secret: str | None) -> dict[str, Any] | None:
    if not secret:
        return None
    row = query_one(
        "SELECT id, name, machine_id, is_active FROM api_keys WHERE key_hash = ?",
        (_hash(secret.strip()),),
    )
    if row is None or not row["is_active"]:
        return None
    # Update last_used dibatasi: jangan nulis DB tiap request kalau belum 60 detik.
    execute("UPDATE api_keys SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR"
            " last_used_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-60 seconds'))",
            (now_iso(), row["id"]))
    return dict(row)


def list_keys() -> list[dict[str, Any]]:
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "key_prefix": row["key_prefix"],
            "machine_id": row["machine_id"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "last_used_at": row["last_used_at"],
        }
        for row in query("SELECT * FROM api_keys ORDER BY created_at DESC")
    ]


def set_active(key_id: str, is_active: bool) -> dict[str, Any] | None:
    execute("UPDATE api_keys SET is_active = ? WHERE id = ?", (1 if is_active else 0, key_id))
    row = query_one("SELECT id, name, is_active FROM api_keys WHERE id = ?", (key_id,))
    return dict(row) if row else None


def rename(key_id: str, name: str) -> dict[str, Any] | None:
    execute("UPDATE api_keys SET name = ? WHERE id = ?", (name.strip(), key_id))
    row = query_one("SELECT id, name FROM api_keys WHERE id = ?", (key_id,))
    return dict(row) if row else None


def delete_key(key_id: str) -> bool:
    return execute("DELETE FROM api_keys WHERE id = ?", (key_id,)) > 0


def extract_client_key(
    *, authorization: str | None = None, x_api_key: str | None = None,
    x_goog_api_key: str | None = None, query_key: str | None = None,
) -> str | None:
    """Extraction order is the same as 9router dashboardGuard.js:141-149."""
    if authorization:
        value = authorization.strip()
        if value.lower().startswith("bearer "):
            return value[7:].strip() or None
        return value or None
    for candidate in (x_api_key, x_goog_api_key, query_key):
        if candidate:
            return candidate.strip()
    return None


def id_of(secret: str | None) -> str | None:
    row = verify(secret)
    return row["id"] if row else None
