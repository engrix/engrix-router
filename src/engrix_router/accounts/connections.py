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
from engrix_router.core import errors
from engrix_router.core import logs as applog
from engrix_router.accounts import health
from engrix_router.transport import proxy
from engrix_router.core.types import Candidate, Credentials, ProviderDef
from engrix_router.core.ids import new_id
from engrix_router.core import tokens

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


def _derive_from_token(claims: dict[str, Any]) -> dict[str, Any]:
    """psd fields the token itself already answers.

    Kept separate from the operator's provider_specific blob so an import or a
    dashboard edit cannot silently drop them: `uid` is what the desktop client
    sends as its account header, and `token_claims` is the audit trail that makes
    "why did this account stop working" answerable without re-deriving the JWT.
    """
    derived: dict[str, Any] = {}
    subject = tokens.subject(claims)
    if subject:
        derived["uid"] = subject
    issued = tokens.issued_iso(claims)
    if issued:
        derived["token_issued_at"] = issued
    summary = tokens.public_summary(claims)
    if summary:
        derived["token_claims"] = summary
    return derived


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
    # Token diperiksa SEBELUM disimpan: bentuk komposit "<akun>+<jwt>" dipecah,
    # lalu expires_at dan identitas diambil dari claim-nya sendiri. Tanpa langkah
    # ini setiap akun OAuth masuk DB dengan expires_at NULL -- dan pemeriksaan
    # kedaluwarsa tidak akan pernah bisa jalan (lihat Credentials.is_expired).
    bare_token, claims = tokens.normalise(access_token)
    psd = dict(provider_specific or {})
    derived = _derive_from_token(claims)
    for key, value in derived.items():
        psd.setdefault(key, value)
    cred = {
        "api_key": api_key,
        "access_token": bare_token or access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at or tokens.expiry_iso(claims),
    }
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
    psd_touched = False
    # Token yang di-paste ulang harus diperlakukan sama seperti saat create():
    # dipecah dari bentuk komposit, lalu expires_at dan identitas diambil dari
    # claim. Tanpa ini, "Edit → paste token baru" akan tetap menyimpan
    # expires_at lama yang sudah basi.
    if patch.get("access_token"):
        bare_token, claims = tokens.normalise(patch["access_token"])
        patch = {**patch, "access_token": bare_token or patch["access_token"]}
        for key, value in _derive_from_token(claims).items():
            if value not in (None, {}, ""):
                psd[key] = value
                psd_touched = True
        if not patch.get("expires_at"):
            derived_expiry = tokens.expiry_iso(claims)
            if derived_expiry:
                patch = {**patch, "expires_at": derived_expiry}
    for key in ("api_key", "access_token", "refresh_token", "expires_at"):
        if key in patch and patch[key] is not None:
            cred[key] = patch[key]
            cred_touched = True
    if isinstance(patch.get("provider_specific"), dict):
        psd.update(patch["provider_specific"])  # shallow merge, sama seperti 9router
        psd_touched = True
    if columns or cred_touched or psd_touched:
        columns["updated_at"] = now_iso()
        sets = ", ".join(f"{key} = ?" for key in columns)
        params = list(columns.values())
        if cred_touched:
            params.append(_dump(cred))
            sets += ", cred_json = ?"
        if psd_touched:
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
    # Baris lama (hasil migrasi 9router) menyimpan token tanpa expires_at.
    # Tanggalnya tetap bisa dijawab token itu sendiri, jadi dashboard tidak perlu
    # menampilkan "kosong" untuk akun yang sebenarnya punya masa berlaku jelas.
    expires_at = cred.get("expires_at")
    if not expires_at:
        expires_at = tokens.expiry_iso(tokens.decode_claims(cred.get("access_token")))
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
        "expires_at": expires_at,
    }


def list_for_provider(provider: str | None = None, *, include_health: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM connections"
    params: tuple[Any, ...] = ()
    if provider:
        sql += " WHERE provider = ?"
        params = (provider,)
    sql += " ORDER BY provider, priority, created_at"
    rows = [_public_view(dict(row)) for row in query(sql, params)]
    if include_health and rows:
        # Satu query untuk seluruh baris. Versi sebelumnya memanggil health.get()
        # di dalam loop, jadi daftar akun berubah jadi N+1 query -- 19 akun sudah
        # memakan ~39 ms CPU per request, dan biayanya naik linear terhadap
        # jumlah akun.
        states = health.many([item["id"] for item in rows])
        for item in rows:
            item["health"] = states[item["id"]]
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
    # Baris hasil migrasi lama tidak punya expires_at. Tanggalnya tetap bisa
    # dibaca dari token, jadi pemeriksaan kedaluwarsa tidak perlu menunggu
    # sampai baris itu ditulis ulang -- tanpa ini, akun dengan token basi
    # selamanya dianggap hidup dan baru ketahuan setelah vendor menolak.
    expires_at = cred.get("expires_at") or tokens.expiry_iso(tokens.decode_claims(token))
    proxy_url, strict = proxy.resolve(row["proxy_pool_id"], _load(row["psd_json"]))
    return Credentials(
        connection_id=row["id"],
        provider=row["provider"],
        auth_type=row["auth_type"],
        name=row["name"] or row["email"] or row["id"][:8],
        token=token or None,
        refresh_token=cred.get("refresh_token"),
        expires_at=expires_at,
        psd=_load(row["psd_json"]),
        proxy_url=proxy_url,
        proxy_strict=strict,
    )


def _locks_for(connection_ids: list[str], model: str, ts: int) -> dict[str, list[dict[str, Any]]]:
    """Every live lock for these connections in ONE query.

    Same predicate as health._locks_of (a lock on this model or a '*' lock on the
    whole account), just not one round-trip per account. Chunked because SQLite
    caps a statement at 999 host parameters on builds older than 3.32, and a
    provider can hold 1500 accounts.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(connection_ids), 500):
        chunk = connection_ids[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = query(
            f"SELECT connection_id, model, locked_until, reason FROM model_locks"  # noqa: S608 -- placeholders only
            f" WHERE connection_id IN ({placeholders})"
            f" AND locked_until > ? AND model IN ('*', ?)",
            tuple(chunk) + (ts, model),
        )
        for row in rows:
            out.setdefault(row["connection_id"], []).append(dict(row))
    return out


def _pools_for(pool_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Active proxy pools keyed by id, one query. Empty set -> no query at all."""
    wanted = sorted({pid for pid in pool_ids if pid})
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = query(
            f"SELECT * FROM proxy_pools WHERE id IN ({placeholders}) AND is_active = 1",  # noqa: S608
            tuple(chunk),
        )
        for row in rows:
            out[row["id"]] = dict(row)
    return out


def _credentials_from_row(row: dict[str, Any], pool: dict[str, Any] | None) -> Credentials:
    """Build Credentials from a row already in memory -- no query, no re-read."""
    cred = _load(row["cred_json"])
    psd = _load(row["psd_json"])
    token = cred.get("access_token") or cred.get("api_key") or ""
    expires_at = cred.get("expires_at") or tokens.expiry_iso(tokens.decode_claims(token))
    proxy_url, strict = proxy.resolve_with_pool(row["proxy_pool_id"], psd, pool)
    return Credentials(
        connection_id=row["id"],
        provider=row["provider"],
        auth_type=row["auth_type"],
        name=row["name"] or row["email"] or row["id"][:8],
        token=token or None,
        refresh_token=cred.get("refresh_token"),
        expires_at=expires_at,
        psd=psd,
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

    Four queries TOTAL, not four per account. The per-account version opened a
    fresh SQLite connection for every health read, lock read and credential load:
    measured on a 10x150 fleet, picking one account cost 601 connections and 1051
    statements, p50 1796 ms, and 50 concurrent pickers collapsed to 2 picks/s.
    Every rule below is still the one health.py owns -- only the fetching changed,
    and health.many() supplies the same defaults get() synthesises so a caller
    cannot tell the two paths apart.
    """
    moment = now_ms() if ts is None else ts
    skip_excluded = exclude or set()

    rows = query(
        "SELECT id, priority, provider, auth_type, name, email, cred_json, psd_json, proxy_pool_id"
        " FROM connections WHERE provider = ? AND is_active = 1 ORDER BY priority, created_at",
        (definition.id,),
    )
    if not rows:
        return [], []

    ids = [row["id"] for row in rows]
    states = health.many(ids)
    locks = _locks_for(ids, model, moment)
    pools = _pools_for([row["proxy_pool_id"] for row in rows])

    ok: list[Candidate] = []
    skipped: list[dict[str, Any]] = []

    for row in rows:
        connection_id = row["id"]
        if connection_id in skip_excluded:
            skipped.append({"connection_id": connection_id, "reason": "already tried in this request"})
            continue

        state = states[connection_id]
        account_locks = locks.get(connection_id, [])
        wildcard_locked = any(item["model"] == health.ACCOUNT_WILDCARD for item in account_locks)

        # Aturan 1: status pemblokir. Sama persis dengan health.is_available(),
        # hanya saja satu putaran seleksi TIDAK boleh MENULIS baris health untuk
        # akun yang cuma sedang dipertimbangkan -- update self-heal tetap di jalur
        # tulis health.
        if state["test_status"] in health.BLOCKING_STATUSES:
            healable = False
            if (state["test_status"] == health.STATUS_NEEDS_REAUTH
                    and state["error_class"] == errors.CLASS_ACCOUNT_RESTRICTED):
                healable = not wildcard_locked
            elif state["test_status"] == health.STATUS_UNAVAILABLE:
                rate_ok = not (state["rate_limited_until"] and state["rate_limited_until"] > moment)
                healable = (health.get_bool("health.self_heal_unavailable") and rate_ok
                            and not wildcard_locked)
            if not healable:
                skipped.append({
                    "connection_id": connection_id,
                    "reason": f"locked/{state['test_status']}",
                    "until_ms": state["rate_limited_until"],
                })
                continue

        # Aturan 2: jendela rate-limit.
        limited_until = state["rate_limited_until"]
        if limited_until and limited_until > moment:
            skipped.append({
                "connection_id": connection_id,
                "reason": f"locked/{state['error_class'] or 'rate_limited'}",
                "until_ms": limited_until,
            })
            continue

        # Aturan 3: kunci model (model ini, atau '*' yang menutup seluruh akun).
        if account_locks:
            worst = max(account_locks, key=lambda item: item["locked_until"])
            skipped.append({
                "connection_id": connection_id,
                "reason": f"locked/{worst.get('reason') or 'locked'}",
                "until_ms": worst["locked_until"],
            })
            continue

        creds = _credentials_from_row(row, pools.get(row["proxy_pool_id"]))
        if creds.is_expired:
            skipped.append({"connection_id": connection_id, "reason": "token expired (re-auth needed)"})
            continue
        if not creds.token and creds.auth_type != "none":
            skipped.append({"connection_id": connection_id, "reason": "credentials are empty"})
            continue

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


def export_rows() -> list[dict[str, Any]]:
    """Every connection WITH its credentials, for the backup file.

    Dashboard views are stripped by design (SAFE_CRED_KEYS / _public_view); this
    is the one read path that must carry secrets, because the document it feeds
    is the only thing that survives a reinstall with the fleet intact. It is
    reachable exclusively through the admin API (fail-closed without a token) and
    is never written to a log line.

    Field names follow the connection row plus a flattened credential block, so
    a restore can round-trip through create()/update() without guessing.
    """
    out: list[dict[str, Any]] = []
    for row in query("SELECT * FROM connections ORDER BY provider, priority, created_at"):
        item = dict(row)
        cred = _load(item.pop("cred_json", None))
        psd = _load(item.pop("psd_json", None))
        item["api_key"] = cred.get("api_key")
        item["access_token"] = cred.get("access_token")
        item["refresh_token"] = cred.get("refresh_token")
        item["expires_at"] = cred.get("expires_at")
        item["provider_specific"] = psd
        item["is_active"] = bool(item.get("is_active", 1))
        out.append(item)
    return out
