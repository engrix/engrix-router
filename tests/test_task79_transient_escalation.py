"""Regression TASK-79: transient failures must escalate, and transport must fail over.

Two defects proved against the production log of 2026-10-25:

1. TRANSPORT -> class=unknown -> retry_internally=False.
   runner.py:382 forwards `UpstreamError.kind` as `exception_name`, whose
   vocabulary is "transport" / "stalled" / "timeout" / "http" -- NOT the httpx
   class name. classify() only matched httpx names, so every ConnectError /
   ReadError / RemoteProtocolError fell through to CLASS_UNKNOWN, whose policy
   says retry_internally=False: the request died on the FIRST account while the
   rest of the fleet was untouched. Log evidence: 440 events with
   class=unknown (the second-largest bucket), every body a
   "transport error: ConnectError/ReadError/RemoteProtocolError/ConnectTimeout".

2. TRANSIENT TTL WAS FLAT.
   `health.cooldown_transient_ms` was applied verbatim, so an account the vendor
   had genuinely broken came back "healthy" 2 s later, was picked again
   (fill-first always takes priority 1), and failed again. Log evidence: 89
   consecutive 503s on ACC=qoder-01a10e9c between 09:53:18 and 10:08:33, every
   single line "locked 2s". Escalation now makes a dead account back off, and
   register_success resets the level so a recovered account is not punished.
"""
from __future__ import annotations

import pytest

from engrix_router.accounts import health
from engrix_router.core import errors
from engrix_router.storage import settings
from engrix_router.storage.sqlite import execute, now_ms, query_one


def _conn(cid: str) -> str:
    execute(
        "INSERT OR REPLACE INTO connections(id, provider, node_id, auth_type, name, email,"
        " priority, is_active, cred_json, psd_json, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "qoder", None, "apikey", cid, None, 1, 1, "{}", "{}", now_ms(), now_ms()),
    )
    execute(
        "INSERT OR REPLACE INTO connection_health(connection_id, test_status, updated_at)"
        " VALUES(?,?,?)",
        (cid, health.STATUS_UNKNOWN, now_ms()),
    )
    return cid


def _fail(cid: str, error_class: str, model: str = "qfmodel") -> dict:
    return health.register_error(cid, model, errors.Classified(
        error_class, errors.policy_of(error_class), f"{error_class} (test)"))


# ── 1. transport vocabulary ──────────────────────────────────────────────────

def test_upstream_error_kind_transport_is_not_unknown():
    """The runner passes UpstreamError.kind, not the httpx class name."""
    for kind in ("transport", "stalled"):
        result = errors.classify(text="transport error: ConnectError: [Errno 11001]",
                                 exception_name=kind)
        assert result.error_class == errors.CLASS_UPSTREAM_UNAVAILABLE, (
            f"kind={kind!r} classified as {result.error_class}")


def test_httpx_class_names_still_classify_as_unavailable():
    """The original httpx vocabulary must keep working (runner's generic except)."""
    for name in ("ConnectError", "RemoteProtocolError", "ReadError", "WriteError"):
        assert errors.classify(text="boom", exception_name=name).error_class \
            == errors.CLASS_UPSTREAM_UNAVAILABLE, name
    # Names containing "timeout" already hit rule 9 first and must keep landing
    # on 504 -- that ordering predates this change and is not a regression.
    for name in ("ConnectTimeout", "ReadTimeout", "PoolTimeout"):
        assert errors.classify(text="boom", exception_name=name).error_class \
            == errors.CLASS_UPSTREAM_TIMEOUT, name


def test_both_transport_classes_allow_failover():
    """Whatever the exact class, a transport failure must never stop the ladder."""
    for error_class in (errors.CLASS_UPSTREAM_UNAVAILABLE, errors.CLASS_UPSTREAM_TIMEOUT):
        assert errors.policy_of(error_class).retry_internally is True, error_class


def test_transport_class_may_fail_over_to_the_next_account():
    """retry_internally is what lets the runner try the next candidate."""
    assert errors.policy_of(errors.CLASS_UPSTREAM_UNAVAILABLE).retry_internally is True
    # class=unknown adalah bucket tempat SEMUA kegagalan tak terklasifikasi jatuh
    # (464 baris di log 2026-10-25, seluruhnya 'transport error: ...'). Kalau kelas
    # ini menolak retry, satu DNS hiccup mematikan request di akun pertama.
    assert errors.policy_of(errors.CLASS_UNKNOWN).retry_internally is True


def test_production_message_shape_is_covered():
    """The exact string in the log, with no exception_name at all."""
    result = errors.classify(
        text="transport error: ConnectError: [Errno 11004] getaddrinfo failed")
    assert result.error_class == errors.CLASS_UPSTREAM_UNAVAILABLE


# ── 2. transient backoff escalation ─────────────────────────────────────────

def test_repeated_transient_failures_escalate_the_lock():
    """2 s flat forever was the sticky-503 bug; the TTL must grow."""
    cid = _conn("t79-escalate")
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")
    settings.set_setting("health.backoff_max_ms", 300_000, actor="test")
    settings.set_setting("health.backoff_max_level", 15, actor="test")

    ttls = []
    for _ in range(4):
        decision = _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)
        ttls.append(decision["lock_ms"])
        # expire the lock so the next failure is a fresh one, not a MAX() merge
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))

    assert ttls == [2_000, 4_000, 8_000, 16_000], ttls


def test_escalation_is_capped_by_backoff_max_ms():
    cid = _conn("t79-cap")
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")
    settings.set_setting("health.backoff_max_ms", 10_000, actor="test")

    ttls = []
    for _ in range(5):
        ttls.append(_fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)["lock_ms"])
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))

    assert ttls == [2_000, 4_000, 8_000, 10_000, 10_000], ttls


def test_success_resets_the_transient_backoff():
    """A recovered account must not carry the punishment forward."""
    cid = _conn("t79-recover")
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")

    for _ in range(3):
        _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))
    assert health.get(cid)["backoff_level"] == 3

    health.register_success(cid, "qfmodel")
    assert health.get(cid)["backoff_level"] == 0
    assert _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)["lock_ms"] == 2_000


def test_timeouts_escalate_too():
    cid = _conn("t79-timeout")
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")
    ttls = []
    for _ in range(3):
        ttls.append(_fail(cid, errors.CLASS_UPSTREAM_TIMEOUT)["lock_ms"])
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))
    assert ttls == [2_000, 4_000, 8_000], ttls


# ── 3. classes that must NOT escalate ───────────────────────────────────────

def test_rate_limit_keeps_its_own_base_and_cap():
    """rate_limited has always used backoff_base_ms -- escalation must not change it."""
    cid = _conn("t79-rate")
    settings.set_setting("health.backoff_base_ms", 2_000, actor="test")
    settings.set_setting("health.backoff_max_ms", 300_000, actor="test")

    ttls = []
    for _ in range(3):
        ttls.append(_fail(cid, errors.CLASS_RATE_LIMIT)["lock_ms"])
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))
    assert ttls == [2_000, 4_000, 8_000], ttls


def test_quota_daily_keeps_its_window_lock():
    """A daily-quota lock is a window, never a doubling timer."""
    cid = _conn("t79-quota")
    first = _fail(cid, errors.CLASS_QUOTA_DAILY)
    second = _fail(cid, errors.CLASS_QUOTA_DAILY)
    assert first["scope"] == "account"
    # both land on the same reset instant, so the second is not longer
    assert second["lock_ms"] <= first["lock_ms"] + 1_000
    row = query_one("SELECT backoff_level FROM connection_health WHERE connection_id=?", (cid,))
    assert row["backoff_level"] == 0


def test_model_scoped_failures_do_not_touch_backoff_level():
    """Only the classes in ESCALATING_CLASSES move the counter."""
    cid = _conn("t79-model-scope")
    _fail(cid, errors.CLASS_MODEL_CONCURRENCY)
    row = query_one("SELECT backoff_level FROM connection_health WHERE connection_id=?", (cid,))
    assert row["backoff_level"] == 0


# ── 4. the level is recorded, so the operator can see it ────────────────────

def test_decision_reports_the_escalated_level():
    cid = _conn("t79-report")
    first = _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)
    second = _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)
    assert first["backoff_level"] == 1
    assert second["backoff_level"] == 2
    assert second["reason"].endswith(":escalated")


# ── 5. the streak belongs to ONE class, it must not leak across classes ─────

def test_transient_streak_does_not_inflate_a_following_rate_limit():
    """Eight 503s then one 429 must cost 2 s, not 2^8 x base = 300 s."""
    cid = _conn("t79-cross")
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")
    settings.set_setting("health.backoff_base_ms", 2_000, actor="test")
    settings.set_setting("health.backoff_max_ms", 300_000, actor="test")

    for _ in range(8):
        _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))
    assert health.get(cid)["backoff_level"] == 8

    decision = _fail(cid, errors.CLASS_RATE_LIMIT)
    assert decision["lock_ms"] == 2_000, decision
    assert decision["backoff_level"] == 1


def test_rate_limit_streak_does_not_inflate_a_following_transient():
    cid = _conn("t79-cross-rev")
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")
    settings.set_setting("health.backoff_base_ms", 2_000, actor="test")

    for _ in range(5):
        _fail(cid, errors.CLASS_RATE_LIMIT)
        execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
                (now_ms() - 1, cid))
    assert health.get(cid)["backoff_level"] == 5

    decision = _fail(cid, errors.CLASS_UPSTREAM_UNAVAILABLE)
    assert decision["lock_ms"] == 2_000, decision
    assert decision["backoff_level"] == 1
