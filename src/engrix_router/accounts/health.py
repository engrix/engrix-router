"""
Connection health: state machine, per-model locks, backoff, drift trip.

9router has no circuit breaker. What it has is an error -> cooldown table
(open-sse/config/errorConfig.js:59-86), per-model locks stored inside the
credential blob (sse/services/auth.js:242-340), and one real breaker that
exists only for antigravity, in RAM (services/antigravityQuota.js:25-29: 60 s
window, threshold 3, 15 min block). This module builds the real thing: a state
machine, locks in their own table, and a global trip that survives a restart.

States:
    unknown -> active -> cooling -> unavailable -> needs_reauth
       ^                      |                       |
       +------- success ------+                       +-- re-test / re-auth --+
`locked` is NOT a connection state; it is a row in model_locks (per model, or
'*' for the whole account). A connection can be `active` while still holding an
unexpired '*' lock.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from engrix_router.storage.sqlite import execute, now_ms, query, query_one, transaction
from engrix_router.core import errors
from engrix_router.core import logs as applog
from engrix_router.storage.settings import get_bool, get_int

STATUS_UNKNOWN = "unknown"
STATUS_ACTIVE = "active"
STATUS_COOLING = "cooling"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NEEDS_REAUTH = "needs_reauth"

ALL_STATUSES = (STATUS_UNKNOWN, STATUS_ACTIVE, STATUS_COOLING, STATUS_UNAVAILABLE, STATUS_NEEDS_REAUTH)
ACCOUNT_WILDCARD = "*"

# Status yang membuat koneksi tidak layak dicoba sama sekali.
BLOCKING_STATUSES = frozenset({STATUS_UNAVAILABLE, STATUS_NEEDS_REAUTH})


def ensure(connection_id: str, *, ts: int | None = None) -> None:
    moment = now_ms() if ts is None else ts
    execute(
        "INSERT INTO connection_health(connection_id, test_status, updated_at)"
        " VALUES(?,?,?) ON CONFLICT(connection_id) DO NOTHING",
        (connection_id, STATUS_UNKNOWN, moment),
    )


def get(connection_id: str) -> dict[str, Any]:
    row = query_one("SELECT * FROM connection_health WHERE connection_id = ?", (connection_id,))
    if row is None:
        return {
            "connection_id": connection_id,
            "test_status": STATUS_UNKNOWN,
            "error_code": None,
            "error_class": None,
            "last_error": None,
            "last_error_at": None,
            "backoff_level": 0,
            "rate_limited_until": None,
            "last_used_at": None,
            "consecutive_use_count": 0,
            "last_probe_at": None,
        }
    return dict(row)


def for_provider(provider: str) -> dict[str, dict[str, Any]]:
    rows = query(
        "SELECT h.* FROM connection_health h JOIN connections c ON c.id = h.connection_id"
        " WHERE c.provider = ?",
        (provider,),
    )
    return {row["connection_id"]: dict(row) for row in rows}


def _locks_of(connection_id: str, model: str, ts: int) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in query(
            "SELECT model, locked_until, reason FROM model_locks"
            " WHERE connection_id = ? AND locked_until > ? AND model IN ('*', ?)",
            (connection_id, ts, model),
        )
    ]


def is_available(connection_id: str, model: str, *, ts: int | None = None) -> bool:
    """May it be tried now? Combines connection state, locks and rate limit."""
    moment = now_ms() if ts is None else ts
    health = get(connection_id)
    if health["test_status"] in BLOCKING_STATUSES:
        # # Self-heal (audit TASK-45 #2): unavailable itu status NON-permanen
        # # -- dibarengi lock berwaktu. Kalau lock '*' sudah expired dan
        # # rate-limit juga sudah lewat, status boleh pulang sendiri ke
        # # cooling; kalau tidak, satu error quota_daily jam 14:00 bikin
        # # akun mati selamanya sampai admin re-test manual.
        if health["test_status"] == STATUS_UNAVAILABLE:
            heal = get_bool("health.self_heal_unavailable")
            rate_ok = not (health["rate_limited_until"] and health["rate_limited_until"] > moment)
            if heal and rate_ok and not _locks_of(connection_id, ACCOUNT_WILDCARD, moment):
                execute(
                    "UPDATE connection_health SET test_status=?, updated_at=? WHERE connection_id=?",
                    (STATUS_COOLING, moment, connection_id),
                )
                applog.info("health", f"self-heal {connection_id}: unavailable -> cooling, all locks expired")
                health["test_status"] = STATUS_COOLING
            else:
                return False
        else:
            return False
    limited_until = health["rate_limited_until"]
    if limited_until and limited_until > moment:
        return False
    return not _locks_of(connection_id, model, moment)


def locked_reason(connection_id: str, model: str, *, ts: int | None = None) -> dict[str, Any] | None:
    moment = now_ms() if ts is None else ts
    locks = _locks_of(connection_id, model, moment)
    if locks:
        return max(locks, key=lambda item: item["locked_until"])
    health = get(connection_id)
    if health["test_status"] in BLOCKING_STATUSES:
        return {"model": ACCOUNT_WILDCARD, "locked_until": health["rate_limited_until"] or 0,
                "reason": f"status={health['test_status']} class={health['error_class']}"}
    limited_until = health["rate_limited_until"]
    if limited_until and limited_until > moment:
        return {"model": model, "locked_until": limited_until, "reason": health["error_class"] or "rate_limited"}
    return None


def gc_expired(connection_id: str | None = None, *, ts: int | None = None) -> int:
    """
    Expired locks are dropped when read (lazy, no timer).

    Same as 9router's accountFallback.js:167-194, which cleans locks on every
    selection instead of running a sweeper.

    """
    moment = now_ms() if ts is None else ts
    if connection_id:
        return execute("DELETE FROM model_locks WHERE connection_id = ? AND locked_until <= ?",
                       (connection_id, moment))
    return execute("DELETE FROM model_locks WHERE locked_until <= ?", (moment,))


def reconcile_quota_locks(connection_id: str, *, ts: int | None = None) -> int:
    """Vendor quota says there is room -> a quota_daily lock is a false positive, drop it.

    ZCode edge can emit code 1005 "exceed quota limit" for an account whose
    entitlement endpoint still reports quota left (measured TASK-57: fresh
    account zeno, vendor readings used=0 remaining=100%, one 1005 right after
    its first request locked the whole account '*' until UTC midnight). The
    quota snapshot is the vendor's own accounting, so it wins over the one-shot
    edge error. Self-correcting: if the edge really meant it, the next request
    1005s again and re-locks.

    Only releases when EVERY measurable scope of the newest snapshot is above
    the floor (unlimited scopes are ignored). A snapshot older than the lock is
    not evidence. Returns number of locks released.
    """
    moment = now_ms() if ts is None else ts
    locks = query(
        "SELECT id, created_at FROM model_locks"
        " WHERE connection_id = ? AND model = ? AND locked_until > ?"
        " AND reason LIKE 'quota_daily%'",
        (connection_id, ACCOUNT_WILDCARD, moment),
    )
    if not locks:
        return 0
    newest = query_one(
        "SELECT MAX(fetched_at) AS at FROM quota_snapshots WHERE connection_id = ?",
        (connection_id,),
    )
    fetched_at = int(newest["at"]) if newest and newest["at"] else 0
    if fetched_at < max(int(lock["created_at"]) for lock in locks):
        return 0  # snapshot lebih tua dari error yang ngunci -- bukan bukti
    readings = query(
        "SELECT remaining_pct, unlimited FROM quota_snapshots"
        " WHERE connection_id = ? AND fetched_at = ?",
        (connection_id, fetched_at),
    )
    measurable = [r for r in readings if not r["unlimited"] and r["remaining_pct"] is not None]
    if not measurable:
        return 0
    floor = get_int("health.quota_reconcile_min_pct")
    if min(float(r["remaining_pct"]) for r in measurable) < floor:
        return 0
    released = execute(
        "DELETE FROM model_locks WHERE connection_id = ? AND model = ? AND locked_until > ?"
        " AND reason LIKE 'quota_daily%'",
        (connection_id, ACCOUNT_WILDCARD, moment),
    )
    # unavailable karena quota_daily juga ikut salah tuduhan -> turunkan ke
    # cooling supaya self-heal is_available() tidak tetap memblokir koneksi.
    row = query_one(
        "SELECT test_status, error_class FROM connection_health WHERE connection_id = ?",
        (connection_id,),
    )
    if row and row["test_status"] == STATUS_UNAVAILABLE and row["error_class"] == errors.CLASS_QUOTA_DAILY:
        execute(
            "UPDATE connection_health SET test_status = ?, error_class = NULL,"
            " error_code = NULL, updated_at = ? WHERE connection_id = ?",
            (STATUS_COOLING, moment, connection_id),
        )
    if released:
        applog.info("health", f"reconcile {connection_id[:8]}: vendor quota has room,"
                              f" released {released} quota_daily lock(s)")
    return released


_METER_EVIDENCE_MAX_AGE_MS = 15 * 60 * 1000


def _quota_meter(connection_id: str, *, not_before: int) -> dict[str, Any]:
    """The vendor's own reading for this connection, or {} when it cannot speak.

    `not_before` rejects readings older than that moment -- a stale meter is not
    evidence about a later failure. Used both ways: to refuse an account lock the
    vendor's own quota contradicts, and to lock until the VENDOR's period end
    (ZCode's is 15:59:59 UTC, 8 hours off the UTC-midnight guess we used to make).

    """
    newest = query_one("SELECT MAX(fetched_at) AS at FROM quota_snapshots WHERE connection_id = ?",
                       (connection_id,))
    fetched_at = int(newest["at"]) if newest and newest["at"] else 0
    if not fetched_at or fetched_at < not_before:
        return {}
    rows = query("SELECT remaining_pct, unlimited, reset_at FROM quota_snapshots"
                 " WHERE connection_id = ? AND fetched_at = ?", (connection_id, fetched_at))
    measurable = [r for r in rows if not r["unlimited"] and r["remaining_pct"] is not None]
    if not measurable:
        return {}
    resets = [int(r["reset_at"]) for r in rows if r["reset_at"]]
    return {"min_pct": min(float(r["remaining_pct"]) for r in measurable),
            "reset_at_ms": max(resets) if resets else None}


def _lock_ms_for(classified: errors.Classified, *, now_ts: int,
                 connection_id: str | None = None) -> tuple[str, int, str]:
    """
    (scope, ttl_ms, reason) for one policy. The table lives in this one place.

    quota_window ends at the vendor's own reset instant: the classified error's
    if it carries one, else the newest meter reading's period end. Only when
    neither exists does it fall back to the UTC-day guess -- code 110 follows a
    calendar we do not own, and ZCode's is 15:59:59 UTC, not 00:00.

    """
    policy = classified.policy
    scope = "model"
    if policy.lock == "none":
        return ("none", 0, "policy: not locked")
    if policy.lock == "auth":
        ttl = get_int("health.cooldown_auth_ms")
        scope = "account"
    elif policy.lock == "short":
        # Vendor kadang ngirim sendiri berapa lama harus nunggu (10605 ->
        # retryAfterSeconds). Dengarin dia kalau lebih lama dari default.
        ttl = max(get_int("health.cooldown_short_ms"),
                  int((classified.retry_after_s or 0) * 1000))
    elif policy.lock == "transient":
        ttl = get_int("health.cooldown_transient_ms")
    elif policy.lock == "backoff":
        # ttl dihitung pemanggil dari backoff_level koneksi (rate limit naik
        # eksponensial 2^level, cap health.backoff_max_ms -- sama seperti
        # accountFallback.js:9-13 di 9router).
        return ("backoff", 0, "policy: backoff eksponensial")
    elif policy.lock == "quota_window":
        until = int(classified.reset_at.timestamp() * 1000) if classified.reset_at else None
        if not until and connection_id:
            until = _quota_meter(connection_id, not_before=now_ts - _METER_EVIDENCE_MAX_AGE_MS).get("reset_at_ms")
        if not until:
            until = now_ts + get_int("health.cooldown_auth_ms")
        return ("account", max(1000, until - now_ts), "daily quota until reset")
    elif policy.lock == "model":
        ttl = int((classified.retry_after_s or 3600) * 1000)
        scope = "model"
    elif policy.lock == "anti_abuse":
        # Edge menolak BENTUK body: yang dikunci model itu sebentar, bukan akun
        # sampai reset kalender (TASK-48). Akun harus tetap boleh dicoba lagi
        # dengan body yang betul.
        return ("model", get_int("health.anti_abuse_cooldown_ms"), "anti-abuse shape rejection")
    elif policy.lock == "drift":
        ttl = 15 * 60 * 1000
        scope = "account"
    else:
        ttl = get_int("health.cooldown_transient_ms")
    return (scope, ttl, f"policy:{policy.lock}")


def register_error(connection_id: str, model: str, classified: errors.Classified,
                   *, ts: int | None = None) -> dict[str, Any]:
    """Record a failure and apply the matching lock; returns the decision for logging."""
    moment = now_ms() if ts is None else ts
    ensure(connection_id, ts=moment)
    health = get(connection_id)
    error_class = classified.error_class

    scope, ttl_or_level, why = _lock_ms_for(classified, now_ts=moment, connection_id=connection_id)
    if error_class == errors.CLASS_RATE_LIMIT:
        level = min(get_int("health.backoff_max_level"), int(health["backoff_level"]) + 1)
        ttl = min(get_int("health.backoff_max_ms"), get_int("health.backoff_base_ms") * (2 ** (level - 1)))
        backoff_level = level
    else:
        ttl = ttl_or_level
        backoff_level = int(health["backoff_level"])

    # Satu "quota habis" dari edge bukan fakta kuota kalau meteran vendor bilang
    # masih ada sisa: itu edge yang nolak, bukan akun yang kering. Tanpa guard ini
    # akun BARU dengan 3.000.000 utuh ke-bench sampai reset (zeno, 2026-10-10
    # 21:44:53 -- 1 detik setelah request pertama). Hukumannya turun jadi
    # per-model sebentar; kalau memang habis, request berikutnya ngunci beneran.
    demoted = False
    if error_class == errors.CLASS_QUOTA_DAILY and scope == "account":
        meter = _quota_meter(connection_id, not_before=moment - _METER_EVIDENCE_MAX_AGE_MS)
        floor = get_int("health.quota_reconcile_min_pct")
        if meter and meter["min_pct"] >= floor:
            scope = "model"
            ttl = get_int("health.anti_abuse_cooldown_ms")
            why = f"quota_daily denied by vendor meter ({meter['min_pct']:.1f}% left)"
            demoted = True

    if scope == "none":
        execute(
            "UPDATE connection_health SET error_code=?, error_class=?, last_error=?, last_error_at=?,"
            " updated_at=? WHERE connection_id=?",
            (classified.vendor_code, error_class, classified.message[:900], moment, moment, connection_id),
        )
        return {"locked": False, "scope": "none", "lock_ms": 0, "reason": why,
                "error_class": error_class, "backoff_level": backoff_level}

    until = moment + max(1000, ttl)
    if error_class == errors.CLASS_CREDENTIAL_DEAD:
        status = STATUS_NEEDS_REAUTH
    elif error_class in (errors.CLASS_QUOTA_DAILY, errors.CLASS_PROTOCOL_DRIFT) and not demoted:
        status = STATUS_UNAVAILABLE
    else:
        status = STATUS_COOLING
    with transaction() as db:
        db.execute(
            "UPDATE connection_health SET test_status=?, error_code=?, error_class=?, last_error=?,"
            " last_error_at=?, backoff_level=?, rate_limited_until=?, updated_at=?"
            " WHERE connection_id=?",
            (status, classified.vendor_code, error_class, classified.message[:900], moment,
             backoff_level, until if error_class == errors.CLASS_RATE_LIMIT else health["rate_limited_until"],
             moment, connection_id),
        )
        lock_model = ACCOUNT_WILDCARD if scope == "account" else model
        db.execute(
            "INSERT INTO model_locks(connection_id, model, locked_until, reason, created_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(connection_id, model) DO UPDATE SET"
            " locked_until = MAX(model_locks.locked_until, excluded.locked_until),"
            " reason = excluded.reason",
            (connection_id, lock_model, until, f"{error_class}:{why}"[:200], moment),
        )
    applog.warn(
        applog.NS_HEALTH,
        f"koneksi {connection_id[:8]} lock {lock_model} {ttl // 1000}s class={error_class}",
    )
    if error_class == errors.CLASS_PROTOCOL_DRIFT:
        maybe_trip_drift(connection_id, classified)
    return {"locked": True, "scope": scope, "lock_ms": until - moment, "reason": why,
            "error_class": error_class, "status": status, "backoff_level": backoff_level}


def register_success(connection_id: str, *, ts: int | None = None, counted: bool = True) -> None:
    """
    Success resets backoff, marks the connection active and clears its locks.

    Success clears a '*' lock too, but not a per-model lock (a model limit is
    specific; a healthy call to another model does not lift it).

    """
    moment = now_ms() if ts is None else ts
    ensure(connection_id, ts=moment)
    with transaction() as db:
        db.execute(
            "UPDATE connection_health SET test_status=?, error_code=NULL, error_class=NULL,"
            " backoff_level=0, rate_limited_until=NULL, last_used_at=?, updated_at=?"
            " WHERE connection_id=?",
            (STATUS_ACTIVE, moment, moment, connection_id),
        )
        if counted:
            db.execute(
                "UPDATE connection_health SET consecutive_use_count = consecutive_use_count + 1,"
                " updated_at=? WHERE connection_id=?",
                (moment, connection_id),
            )
        db.execute("DELETE FROM model_locks WHERE connection_id = ?", (connection_id,))


def touch_used(connection_id: str, *, bump_count: bool, ts: int | None = None) -> None:
    moment = now_ms() if ts is None else ts
    ensure(connection_id, ts=moment)
    if bump_count:
        execute(
            "UPDATE connection_health SET last_used_at=?, consecutive_use_count="
            " consecutive_use_count + 1, updated_at=? WHERE connection_id=?",
            (moment, moment, connection_id),
        )
    else:
        execute("UPDATE connection_health SET consecutive_use_count=1, last_used_at=?, updated_at=?"
                " WHERE connection_id=?", (moment, moment, connection_id))


def set_test_status(connection_id: str, status: str, *, error: str | None = None,
                    latency_ms: int | None = None, tier: str | None = None) -> dict[str, Any]:
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown status: {status}")
    moment = now_ms()
    ensure(connection_id, ts=moment)
    if status == STATUS_ACTIVE:
        # Padanan resetHealthStateOnActivation (connectionsRepo.js:15-33):
        # re-test yang lulus = semua state kegagalan dibersihin.
        reset_on_activation(connection_id)
        return get(connection_id)
    execute(
        "UPDATE connection_health SET test_status=?, last_error=?, last_error_at=?,"
        " last_probe_at=?, updated_at=? WHERE connection_id=?",
        (status, (error or "")[:900] or None, moment, moment, moment, connection_id),
    )
    return get(connection_id)


def mark_probe(connection_id: str, result: dict[str, Any]) -> dict[str, Any]:
    moment = now_ms()
    ensure(connection_id, ts=moment)
    status = STATUS_ACTIVE if result.get("ok") else STATUS_COOLING
    execute(
        "UPDATE connection_health SET last_probe_at=?, updated_at=?, test_status=?,"
        " last_error=?, last_error_at=?, error_class=? WHERE connection_id=?",
        (moment, moment, status, (result.get("error") or "")[:900] or None,
         moment if not result.get("ok") else None,
         None if result.get("ok") else "probe_failed", connection_id),
    )
    if result.get("ok"):
        execute("UPDATE connection_health SET error_code=NULL, backoff_level=0 WHERE connection_id=?",
                (connection_id,))
    return get(connection_id)


def reset_on_activation(connection_id: str) -> None:
    moment = now_ms()
    with transaction() as db:
        db.execute(
            "UPDATE connection_health SET test_status=?, error_code=NULL, error_class=NULL,"
            " last_error=NULL, last_error_at=NULL, backoff_level=0, rate_limited_until=NULL,"
            " updated_at=? WHERE connection_id=?",
            (STATUS_ACTIVE, moment, connection_id),
        )
        db.execute("DELETE FROM model_locks WHERE connection_id = ?", (connection_id,))


# ── trip global protocol drift ───────────────────────────────────────────────
def maybe_trip_drift(connection_id: str, classified: errors.Classified) -> None:
    """Several sig_invalid in a row means the upstream changed, not that our traffic is bad."""
    from engrix_router.storage import settings

    threshold = get_int("health.protocol_drift_threshold")
    count = 0
    for row in query(
        "SELECT connection_id FROM connection_health WHERE error_class=? AND last_error_at > ?",
        (errors.CLASS_PROTOCOL_DRIFT, now_ms() - 10 * 60 * 1000),
    ):
        count += 1
    if count >= max(1, threshold):
        settings.set_setting("health.drift_until_ms", now_ms() + 15 * 60 * 1000,
                                   actor="health")
        settings.set_setting("health.drift_reason",
                                   f"{count}x protocol_drift (terakhir {connection_id[:8]}: "
                                   f"{classified.message[:120]})",
                                   actor="health")
        applog.error(applog.NS_HEALTH,
                    f"PROTOCOL DRIFT trip: {count}x dalam 10 menit -> upstream call dibekukan 15 menit")


def drift_state(ts: int | None = None) -> dict[str, Any]:
    from engrix_router.storage import settings

    until = int(settings.get("health.drift_until_ms") or 0)
    moment = now_ms() if ts is None else ts
    return {
        "tripped": until > moment,
        "until_ms": until,
        "until_iso": datetime.fromtimestamp(until / 1000, timezone.utc).isoformat() if until else None,
        "reason": settings.get("health.drift_reason") or "",
    }


def clear_drift() -> None:
    from engrix_router.storage import settings

    settings.set_setting("health.drift_until_ms", 0, actor="health")
    settings.set_setting("health.drift_reason", "", actor="health")
    applog.info(applog.NS_HEALTH, "protocol drift trip dibersihin manual")


def snapshot() -> list[dict[str, Any]]:
    rows = query(
        "SELECT h.*, c.provider, c.name, c.email FROM connection_health h"
        " JOIN connections c ON c.id = h.connection_id ORDER BY c.provider, c.priority"
    )
    out = []
    for row in rows:
        item = dict(row)
        item["locks"] = [
            dict(lock)
            for lock in query(
                "SELECT model, locked_until, reason FROM model_locks"
                " WHERE connection_id = ? AND locked_until > ? ORDER BY locked_until DESC",
                (row["connection_id"], now_ms()),
            )
        ]
        out.append(item)
    return out


def lock_plain_4xx_allowed() -> bool:
    return get_bool("health.lock_plain_4xx")
