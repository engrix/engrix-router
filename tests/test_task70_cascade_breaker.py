"""TASK-70 regressions: anti-abuse shape-cascade circuit breaker.

Normal:  1-2 distinct 3012 accounts do NOT pause the model.
Hard:    3 distinct accounts pause it; pause expires; other models unaffected;
         same account repeating never trips it.
Extreme: window eviction (old hits do not count), settings overrides honored,
         status() reports the right shape, thread-safety smoke under parallel notes.
"""
from __future__ import annotations

import threading

from engrix_router.accounts import cascade
from engrix_router.storage import settings

T = 2_000_000_000_000


def setup_function() -> None:
    cascade._reset()


def teardown_function() -> None:
    cascade._reset()


# ---------- normal ----------

def test_two_distinct_accounts_do_not_pause():
    a = cascade.note_anti_abuse("glm-5.3", "A", ts=T)
    b = cascade.note_anti_abuse("glm-5.3", "B", ts=T + 5_000)
    assert (a, b) == (False, False)
    assert cascade.is_paused("glm-5.3", ts=T + 6_000) == (False, 0)


def test_other_model_unaffected_by_neighbor_trip():
    cascade.note_anti_abuse("glm-5.3", "A", ts=T)
    cascade.note_anti_abuse("glm-5.3", "B", ts=T + 1_000)
    cascade.note_anti_abuse("glm-5.3", "C", ts=T + 2_000)
    assert cascade.is_paused("glm-5.3", ts=T + 3_000)[0] is True
    assert cascade.is_paused("glm-4.5", ts=T + 3_000) == (False, 0)


# ---------- hard ----------

def test_third_distinct_account_trips_pause():
    seq = [cascade.note_anti_abuse("glm-5.3", "A", ts=T),
           cascade.note_anti_abuse("glm-5.3", "B", ts=T + 1_000),
           cascade.note_anti_abuse("glm-5.3", "C", ts=T + 2_000)]
    assert seq == [False, False, True]
    paused, until = cascade.is_paused("glm-5.3", ts=T + 3_000)
    assert paused is True
    assert until >= T + 2_000 + settings.get_int("health.cascade_pause_ms") - 1_000


def test_pause_expires_and_allows_again():
    for i, cid in enumerate(("A", "B", "C")):
        cascade.note_anti_abuse("glm-5.3", cid, ts=T + i)
    pause_ms = settings.get_int("health.cascade_pause_ms")
    # last trip lands at T+2ms -> until = T+2+pause; check strictly past it
    assert cascade.is_paused("glm-5.3", ts=T + pause_ms + 10) == (False, 0)


def test_same_account_repetition_never_trips():
    for i in range(10):
        cascade.note_anti_abuse("glm-5.3", "same-account", ts=T + i * 100)
    assert cascade.is_paused("glm-5.3", ts=T + 2_000) == (False, 0)


# ---------- extreme ----------

def test_window_eviction_old_hits_do_not_count():
    # 2 stale hits outside the 60s window, 1 fresh -> still under threshold
    cascade.note_anti_abuse("glm-5.3", "A", ts=T - 700_000)
    cascade.note_anti_abuse("glm-5.3", "B", ts=T - 650_000)
    tripped = cascade.note_anti_abuse("glm-5.3", "C", ts=T)
    assert tripped is False
    assert cascade.is_paused("glm-5.3", ts=T + 1) == (False, 0)


def test_settings_override_tightens_threshold(monkeypatch):
    real_get_int = cascade.get_int

    def fake_get_int(key: str) -> int:
        if key == "health.cascade_min_accounts":
            return 2
        return real_get_int(key)

    monkeypatch.setattr(cascade, "get_int", fake_get_int)
    a = cascade.note_anti_abuse("glm-5.3", "A", ts=T)
    b = cascade.note_anti_abuse("glm-5.3", "B", ts=T + 1_000)
    assert (a, b) == (False, True)


def test_status_shape():
    for i, cid in enumerate(("A", "B", "C")):
        cascade.note_anti_abuse("glm-5.3", cid, ts=T + i)
    st = cascade.status("glm-5.3", ts=T + 100)
    assert st["paused"] is True
    assert st["distinct_3012_accounts_in_window"] == 3
    assert st["model"] == "glm-5.3"
    assert st["until_ms"] > 0


def test_parallel_notes_thread_safety():
    errors: list[str] = []
    def worker(n: int) -> None:
        try:
            for i in range(20):
                cascade.note_anti_abuse("glm-5.3", f"acc-{n}-{i % 7}", ts=T + n * 100 + i)
                cascade.is_paused("glm-5.3", ts=T + n * 100 + i)
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
