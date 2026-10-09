"""
CRUD for upstream connections + loading credentials for routing.

Counterpart of 9router's lib/db/repos/connectionsRepo.js plus the load half of
sse/services/auth.js. Health state lives in accounts/health.py instead (see D1
in schema.sql): 9router stores testStatus/errorCode/backoffLevel/lastUsedAt/
modelLock_<model> in the same JSON as the access_token, so every lock write
rewrites the token.

Secrets live in cred_json. That is not strong encryption - but:
  * list and dashboard output is always stripped (SAFE_CRED_KEYS), the same
    fields 9router strips in app/api/providers/route.js:69-76;
  * the database file sits in data/ (gitignored) and cred_json never reaches a
    log (redaction in core/logs.py).
"""
from __future__ import annotations

import json
from typing import Any

from engrix_router.storage.sqlite import execute, now_iso, now_ms, query, query_one, transaction
from engrix_router.core import logs as applog
from engrix_router.accounts import health
from engrix_router.transport import proxy
from engrix_router.core.types import Candidate, Credentials, ProviderDef
from engrix_router.core.ids import new_id

SAFE_CRED_KEYS = frozenset({"redacted": True})
# Kunci yang boleh dibaca dashboard dari cred_json.
PUBLIC_CRED_MARKERS = ("has_api_key", "has_access_token", "expires_at", "auth_method")


def _dump(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False)


def _load(text: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def next_priority(provider: str) -> int:
    row = query_one("SELECT MAX(priority) AS p FROM connections WHERE provider = ?", (provider,))
    return int(row["p"]) + 1 if row and row["p"] is not None else 1


def create(
    *,
    provider: str,
    auth_type: str = "apikey",
    name: str | None = None,
    email: str | None = None,
    api_key: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: str | None = None,
    priority: int | None = None,
    provider_specific: dict[str, Any] | None = None,
    proxy_pool_id: str | None = None,
    node_id: str | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    if auth_type not in {"apikey", "oauth", "none"}:
        raise ValueError(f"unknown auth_type: {auth_type}")
    if auth_type == "apikey" and not api_key:
        raise ValueError("auth_type=apikey requires an api_key")
    if auth_type == "oauth" and not access_token:
        raise ValueError("auth_type=oauth requires an access_token")
    connection_id = new_id()
    cred = {
        "api_key": api_key,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
    }
    psd = dict(provider_specific or {})
    resolved_priority = int(priority) if priority is not None else next_priority(provider)
    with transaction() as db:
        db.execute(
            "INSERT INTO connections(id, provider, node_id, auth_type, name, email, priority, is_active,"
            " proxy_pool_id, cred_json, psd_json, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (connection_id, provider, node_id, auth_type, name, email, resolved_priority,
             1 if is_active else 0, proxy_pool_id, _dump(cred), _dump(psd), now_iso(), now_iso()),
        )
        db.execute(
            "INSERT INTO connection_health(connection_id, test_status, updated_at) VALUES(?,?,?)",
            (connection_id, health.STATUS_UNKNOWN, now_ms()),
        )
    applog.info(applog.NS_KEYS, f"koneksi dibuat: {provider}/{name or connection_id[:8]} prio={resolved_priority}")
    return get_for_dashboard(connection_id) or {"id": connection_id}


# Field yang boleh di-PUT. Whitelist eksplisit = gak ada mass assignment
# (9router juga whitelist, src/app/api/providers/[id]/route.js:91-128).
UPDATABLE_COLUMNS = {"name", "email", "priority", "is_active", "proxy_pool_id", "node_id", "auth_type"}


def update(connection_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    existing = query_one("SELECT * FROM connections WHERE id = ?", (connection_id,))
    if existing is None:
        return None
    columns: dict[str, Any] = {}
    for key, value in patch.items():
        if key in UPDATABLE_COLUMNS:
            columns[key] = 1 if value is True else 0 if value is False else value
    cred = _load(existing["cred_json"])
    psd = _load(existing["psd_json"])
    cred_touched = False
    for key in ("api_key", "access_token", "refresh_token", "expires_at"):
        if key in patch and patch[key] is not None:
            cred[key] = patch[key]
            cred_touched = True
    if isinstance(patch.get("provider_specific"), dict):
        psd.update(patch["provider_specific"])  # shallow merge, sama seperti 9router
    if columns or cred_touched or isinstance(patch.get("provider_specific"), dict):
        columns["updated_at"] = now_iso()
        sets = ", ".join(f"{key} = ?" for key in columns)
        params = list(columns.values())
        if cred_touched:
            params.append(_dump(cred))
            sets += ", cred_json = ?"
        if isinstance(patch.get("provider_specific"), dict):
            params.append(_dump(psd))
            sets += ", psd_json = ?"
        params.append(connection_id)
        execute(f"UPDATE connections SET {sets} WHERE id = ?", params)  # noqa: S608 -- key dari whitelist
    if patch.get("test_status"):
        health.set_test_status(connection_id, patch["test_status"], error=patch.get("last_error"))
    return get_for_dashboard(connection_id)


def delete(connection_id: str) -> bool:
    """
    Delete a connection; connection_health and model_locks follow via FK CASCADE.

    Health and locks are NOT kept: a row that vanished from the dashboard but
    still cools down silently is worse than a restart.

    """
    return execute("DELETE FROM connections WHERE id = ?", (connection_id,)) > 0


def _public_view(row: dict[str, Any]) -> dict[str, Any]:
    cred = _load(row.get("cred_json"))
    psd = _load(row.get("psd_json"))
    return {
        "id": row["id"],
        "provider": row["provider"],
        "node_id": row.get("node_id"),
        "auth_type": row["auth_type"],
        "name": row.get("name"),
        "email": row.get("email"),
        "priority": row.get("priority"),
        "is_active": bool(row.get("is_active", 1)),
        "proxy_pool_id": row.get("proxy_pool_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "provider_specific": psd,
        # marker, bukan nilai: dashboard boleh tau "ada key-nya" dan kapan mati.
        "has_api_key": bool(cred.get("api_key")),
        "has_access_token": bool(cred.get("access_token")),
        "expires_at": cred.get("expires_at"),
    }


def list_for_provider(provider: str | None = None, *, include_health: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM connections"
    params: tuple[Any, ...] = ()
    if provider:
        sql += " WHERE provider = ?"
        params = (provider,)
    sql += " ORDER BY provider, priority, created_at"
    rows = [_public_view(dict(row)) for row in query(sql, params)]
    if include_health:
        for item in rows:
            item["health"] = health.get(item["id"])
    return rows


def get_for_dashboard(connection_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM connections WHERE id = ?", (connection_id,))
    if row is None:
        return None
    item = _public_view(dict(row))
    item["health"] = health.get(connection_id)
    return item


def load_credentials(connection_id: str) -> Credentials | None:
    row = query_one("SELECT * FROM connections WHERE id = ?", (connection_id,))
    if row is None:
        return None
    cred = _load(row["cred_json"])
    token = cred.get("access_token") or cred.get("api_key") or ""
    proxy_url, strict = proxy.resolve(row["proxy_pool_id"], _load(row["psd_json"]))
    return Credentials(
        connection_id=row["id"],
        provider=row["provider"],
        auth_type=row["auth_type"],
        name=row["name"] or row["email"] or row["id"][:8],
        token=token or None,
        refresh_token=cred.get("refresh_token"),
        expires_at=cred.get("expires_at"),
        psd=_load(row["psd_json"]),
        proxy_url=proxy_url,
        proxy_strict=strict,
    )


def candidates(definition: ProviderDef, model: str, *, exclude: set[str] | None = None,
               ts: int | None = None) -> tuple[list[Candidate], list[dict[str, Any]]]:
    """
    Usable candidates plus the skipped list with reasons (for trace and log).

    The `definition` arrives from the caller instead of being looked up here:
    accounts and providers are one band and must not import each other, and
    resolving "qd/qfmodel means provider qoder" is the pipeline's job. If it
    is unknown, the client already got a 404. Ordering comes from the
    connections table; the selection strategy lives in routing/selector.py.

    """
    rows = query(
        "SELECT id, priority FROM connections WHERE provider = ? AND is_active = 1"
        " ORDER BY priority, created_at",
        (definition.id,),
    )
    skip_excluded = exclude or set()
    ok: list[Candidate] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        connection_id = row["id"]
        if connection_id in skip_excluded:
            skipped.append({"connection_id": connection_id, "reason": "already tried in this request"})
            continue
        if not health.is_available(connection_id, model, ts=ts):
            reason = health.locked_reason(connection_id, model, ts=ts) or {}
            skipped.append({
                "connection_id": connection_id,
                "reason": f"locked/{'status'} -> {reason.get('reason', 'unhealthy')}",
                "until_ms": reason.get("locked_until"),
            })
            continue
        creds = load_credentials(connection_id)
        if creds is None:
            skipped.append({"connection_id": connection_id, "reason": "koneksi hilang"})
            continue
        if creds.is_expired:
            skipped.append({"connection_id": connection_id, "reason": "token expired (re-auth needed)"})
            continue
        if not creds.token and creds.auth_type != "none":
            skipped.append({"connection_id": connection_id, "reason": "credentials are empty"})
            continue
        state = health.get(connection_id)
        ok.append(
            Candidate(
                credentials=creds,
                provider_def=definition,
                priority=int(row["priority"] or 100),
                test_status=state["test_status"],
                last_used_ms=state["last_used_at"],
                consecutive_use_count=int(state["consecutive_use_count"] or 0),
                rate_limited_until_ms=state["rate_limited_until"],
            )
        )
    return ok, skipped


def reorder(provider: str, ordered_ids: list[str]) -> int:
    """Set priority = array order, in one transaction, so there is no half state."""
    moment = now_iso()
    with transaction() as db:
        for index, connection_id in enumerate(ordered_ids, start=1):
            db.execute(
                "UPDATE connections SET priority = ?, updated_at = ?"
                " WHERE id = ? AND provider = ?",
                (index, moment, connection_id, provider),
            )
    return len(ordered_ids)


def usage_summary(connection_id: str, *, days: int = 7) -> dict[str, Any]:
    row = query_one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(total),0) AS tokens,"
        " COALESCE(SUM(cost_usd),0) AS cost, MAX(ts) AS last_ts FROM requests"
        " WHERE connection_id = ? AND ts > (strftime('%s','now') * 1000) - ? * 86400000",
        (connection_id, days),
    )
    return dict(row) if row else {"requests": 0, "tokens": 0, "cost": 0, "last_ts": None}
