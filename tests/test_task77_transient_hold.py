"""TASK-77 regressions: transient hold instead of fail-fast.

Normal:  a short transient lock on the only account -> the runner holds until
         the lock expires and the re-pick succeeds (never a 503).
Hard:    a quota lock is NEVER waited out (fail fast as before); helper never
         holds for skips waiting cannot fix (already tried / mixed reasons).
Extreme: routing.transient_hold_ms=0 disables the hold; budget boundary
         (opening past budget -> 0) and all the helper's guard clauses.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time

import pytest

from engrix_router.accounts import connections, health
from engrix_router.core import errors
from engrix_router.pipeline import runner
from engrix_router.providers import registry
from engrix_router.storage import settings
from engrix_router.storage.sqlite import now_ms

T = 2_000_000_000_000
BODY = {"model": "openai/gpt-test", "messages": [{"role": "user", "content": "hi"}]}


def _seed_lock(connection_id: str, model: str, ttl_ms: int, reason: str) -> None:
    con = sqlite3.connect(os.environ["EROUTER_DB_PATH"])
    try:
        con.execute(
            "INSERT INTO model_locks(connection_id, model, locked_until, reason, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (connection_id, model, now_ms() + ttl_ms, reason, now_ms()),
        )
        con.commit()
    finally:
        con.close()


def _ok_complete(content: str):
    async def complete(self, request):
        return {"id": "t77", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    return complete


@pytest.fixture(autouse=True)
def _hold_defaults():
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="test")
    settings.set_setting("routing.transient_hold_ms", 10_000, actor="test")
    yield
    settings.set_setting("health.cooldown_transient_ms", 30_000, actor="test")
    settings.set_setting("routing.transient_hold_ms", 25_000, actor="test")


# ---------- normal ----------

def test_transient_lock_is_held_out_and_request_succeeds(monkeypatch):
    """The 05:33 qoder storm scenario, fixed: hold ~2s, re-pick, 200."""
    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete",
                        _ok_complete("AFTER-HOLD"))
    row = connections.create(provider="openai", name="t77-transient", api_key="sk-t77a")
    health.register_error(row["id"], "gpt-test", errors.Classified(
        error_class="upstream_unavailable", policy=errors.policy_of("upstream_unavailable"),
        message="All backends failed"))

    t0 = time.monotonic()
    result = asyncio.run(runner.run_chat(BODY))
    elapsed = time.monotonic() - t0

    assert result["choices"][0]["message"]["content"] == "AFTER-HOLD"
    assert elapsed >= 1.0, f"runner did not hold (elapsed {elapsed:.2f}s) -- it failed fast"
    assert elapsed <= 8.0, f"hold ran away (elapsed {elapsed:.2f}s)"
    connections.delete(row["id"])


# ---------- hard ----------

def test_quota_lock_is_never_waited_out(monkeypatch):
    """A quota lock is a REAL lock: fail fast with its retry_after, no hold."""
    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete",
                        _ok_complete("SHOULD-NOT-RUN"))
    row = connections.create(provider="openai", name="t77-quota", api_key="sk-t77q")
    _seed_lock(row["id"], "gpt-test", ttl_ms=600_000,
               reason="quota_daily:daily quota until reset")

    t0 = time.monotonic()
    with pytest.raises(runner.RequestRejected) as raised:
        asyncio.run(runner.run_chat(BODY))
    elapsed = time.monotonic() - t0

    assert raised.value.classified.error_class == "all_locked"
    assert (raised.value.classified.retry_after_s or 0) >= 500
    assert elapsed < 1.0, f"runner waited out a quota lock (elapsed {elapsed:.2f}s)"
    connections.delete(row["id"])


def test_helper_never_holds_for_unfixable_skips(monkeypatch):
    """'already tried' and mixed-reason skips cannot be fixed by waiting."""
    monkeypatch.setattr(runner, "now_ms", lambda: T)
    assert runner._transient_hold_wait_s(
        [{"reason": "already tried in this request"}], opening=T + 1_000) == 0
    assert runner._transient_hold_wait_s(
        [{"reason": "locked/upstream_unavailable:policy:transient", "until_ms": T + 1_000},
         {"reason": "locked/quota_daily:daily quota until reset", "until_ms": T + 60_000}],
        opening=T + 1_000) == 0
    assert runner._transient_hold_wait_s([], opening=T + 1_000) == 0
    assert runner._transient_hold_wait_s(
        [{"reason": "locked/upstream_unavailable:policy:transient"}], opening=None) == 0


# ---------- extreme ----------

def test_zero_budget_disables_the_hold(monkeypatch):
    """routing.transient_hold_ms=0 restores the old fail-fast behaviour."""
    settings.set_setting("routing.transient_hold_ms", 0, actor="test")
    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete",
                        _ok_complete("SHOULD-NOT-RUN"))
    row = connections.create(provider="openai", name="t77-off", api_key="sk-t77x")
    health.register_error(row["id"], "gpt-test", errors.Classified(
        error_class="upstream_unavailable", policy=errors.policy_of("upstream_unavailable"),
        message="All backends failed"))

    t0 = time.monotonic()
    with pytest.raises(runner.RequestRejected) as raised:
        asyncio.run(runner.run_chat(BODY))
    elapsed = time.monotonic() - t0

    assert raised.value.classified.error_class == "all_locked"
    assert elapsed < 1.0, f"hold ran despite budget=0 (elapsed {elapsed:.2f}s)"
    connections.delete(row["id"])


def test_helper_budget_boundary_and_guards(monkeypatch):
    """Opening past the budget -> no hold; opening inside it -> rounded seconds."""
    monkeypatch.setattr(runner, "now_ms", lambda: T)
    skips = [{"reason": "locked/upstream_unavailable:policy:transient"}]
    # opening exactly at the budget boundary is still acceptable
    assert runner._transient_hold_wait_s(skips, opening=T + 10_000) == 10
    # one millisecond past it is not
    assert runner._transient_hold_wait_s(skips, opening=T + 10_001) == 0
    # expired opening -> nothing to wait for
    assert runner._transient_hold_wait_s(skips, opening=T - 1) == 0
    # sub-second waits round UP to at least 1s (never wake before the opening:
    # floor rounding was the N1 bug -- 1978ms -> sleep 1000 -> still locked)
    assert runner._transient_hold_wait_s(skips, opening=T + 500) == 1
    assert runner._transient_hold_wait_s(skips, opening=T + 1_500) == 2
