"""Regression: list endpoints must not issue one query per connection.

The bug this locks down: health.get() was called inside a loop, so every list
request cost N+1 SQLite round-trips on the event loop. On a 19-account fleet
that was ~40 ms of CPU per request, and it grew linearly with the account count.

Two things are asserted here:
  1. query COUNT is constant -- adding connections must not add queries;
  2. the PAYLOAD SHAPE is unchanged -- the dashboard reads these keys, so a
     faster endpoint that drops or renames a field is not a fix, it is an
     outage.
"""
from __future__ import annotations

from typing import Any

import pytest

from engrix_router.accounts import connections, health
from engrix_router.storage import sqlite as db


@pytest.fixture()
def counter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every SQL statement the storage layer executes."""
    seen: list[str] = []
    real_query = db.query
    real_query_one = db.query_one

    def spy_query(sql: str, params: Any = ()) -> list[dict[str, Any]]:
        seen.append(" ".join(sql.split()))
        return real_query(sql, params)

    def spy_query_one(sql: str, params: Any = ()) -> dict[str, Any] | None:
        seen.append(" ".join(sql.split()))
        return real_query_one(sql, params)

    monkeypatch.setattr(health, "query", spy_query)
    monkeypatch.setattr(health, "query_one", spy_query_one)
    monkeypatch.setattr(connections, "query", spy_query)
    monkeypatch.setattr(connections, "query_one", spy_query_one)
    return seen


def _seed(count: int) -> list[str]:
    ids = []
    for index in range(count):
        row = connections.create(
            provider="openai", name=f"acct-{index}", api_key=f"sk-test-{index}",
            priority=index + 1,
        )
        ids.append(row["id"])
    return ids


def test_list_for_provider_issues_one_health_query_regardless_of_account_count(counter):
    _seed(1)
    counter.clear()
    one = connections.list_for_provider("openai")
    queries_with_one = len(counter)

    _seed(11)  # 12 accounts total now
    counter.clear()
    many = connections.list_for_provider("openai")
    queries_with_many = len(counter)

    assert len(one) == 1
    assert len(many) == 12
    assert queries_with_many == queries_with_one, (
        f"query count grew from {queries_with_one} to {queries_with_many} when the "
        "account count grew -- that is the N+1 pattern coming back"
    )
    # The health rows still have to be there, one per connection.
    assert all("health" in item for item in many)


def test_health_many_matches_get_for_every_connection(counter):
    ids = _seed(5)
    health.set_test_status(ids[0], "active")
    health.set_test_status(ids[1], "unavailable", error="boom")

    batched = health.many(ids)
    one_by_one = {connection_id: health.get(connection_id) for connection_id in ids}

    assert batched == one_by_one
    # A connection with no health row must look identical to get()'s default,
    # otherwise callers cannot treat the two paths as interchangeable.
    missing = health.many(["does-not-exist"])["does-not-exist"]
    assert missing["test_status"] == health.STATUS_UNKNOWN
    assert missing["backoff_level"] == 0


def test_health_many_handles_empty_and_duplicate_ids(counter):
    assert health.many([]) == {}

    ids = _seed(2)
    counter.clear()
    result = health.many([ids[0], ids[0], ids[1]])
    assert set(result) == set(ids)
    assert len(counter) == 1, "duplicate ids must not produce duplicate queries"


def test_snapshot_issues_one_lock_query(counter):
    ids = _seed(4)
    for connection_id in ids:
        health.ensure(connection_id)
    counter.clear()
    rows = health.snapshot()
    assert len(rows) == 4
    lock_queries = [sql for sql in counter if "model_locks" in sql]
    assert len(lock_queries) == 1, (
        f"expected exactly one model_locks query, got {len(lock_queries)}"
    )
    assert all(isinstance(row["locks"], list) for row in rows)


def test_snapshot_lock_payload_keeps_its_shape(counter):
    ids = _seed(1)
    health.ensure(ids[0])
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO model_locks(connection_id, model, locked_until, reason, created_at)"
            " VALUES(?,?,?,?,?)",
            (ids[0], "gpt-4o", db.now_ms() + 600_000, "rate_limited", db.now_ms()),
        )
    row = health.snapshot()[0]
    assert row["locks"] == [
        {"model": "gpt-4o", "locked_until": row["locks"][0]["locked_until"],
         "reason": "rate_limited"}
    ], "the lock dict must keep exactly model/locked_until/reason"
    assert "connection_id" not in row["locks"][0], (
        "connection_id was grouped away -- leaking it back changes the payload"
    )
