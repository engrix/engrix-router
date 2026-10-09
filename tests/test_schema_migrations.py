"""Guard: a schema change reaches EXISTING databases, not only fresh ones.

schema.sql describes a new database. The owner's DB is an old one, so every DDL
change has to be a SCHEMA_VERSION bump plus a MIGRATIONS entry -- otherwise the
first boot after the change dies with "schema differs" and the gateway is down for
no reason. These tests build a v1-shaped database by hand and check it comes out v2.
"""
from __future__ import annotations

import sqlite3

import pytest

from engrix_router.core import config
from engrix_router.storage import sqlite as db


@pytest.fixture
def scratch_db(monkeypatch, request):
    """DB terpisah di data/test -- jangan pernah nyentuh DB asli owner."""
    path = config.DATA_DIR / f"migration_{request.node.name}.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    monkeypatch.setattr(config, "DB_PATH", path)
    monkeypatch.setattr(db, "_SCHEMA_READY", False)
    yield path
    if path.exists():
        path.unlink()


def _columns(path, table: str) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def _stamp(path, *, version: str, checksum: str) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)", (version,))
        conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_checksum', ?)",
                     (checksum,))
        conn.commit()
    finally:
        conn.close()


def test_migration_adds_the_new_columns_to_an_old_database(scratch_db):
    db.init_schema()
    assert {"credits", "credits_original"} <= _columns(scratch_db, "requests")
    # buang kolomnya, mundurin versi: persis kondisi DB yang bikin upgrade mati dulu.
    conn = sqlite3.connect(str(scratch_db))
    conn.execute("ALTER TABLE requests DROP COLUMN credits")
    conn.execute("ALTER TABLE requests DROP COLUMN credits_original")
    conn.execute("DELETE FROM schema_meta")
    conn.commit()
    conn.close()
    _stamp(scratch_db, version="1", checksum="stale-checksum-from-v1")
    assert "credits" not in _columns(scratch_db, "requests")

    info = db.init_schema()

    assert {"credits", "credits_original"} <= _columns(scratch_db, "requests")
    assert info["version"] == db.SCHEMA_VERSION
    assert len(info["migrations_applied"]) == len(db.MIGRATIONS[2])


def test_migration_is_idempotent(scratch_db):
    db.init_schema()
    again = db.init_schema()
    assert again["migrations_applied"] == []


def test_database_newer_than_the_build_is_refused(scratch_db):
    db.init_schema()
    _stamp(scratch_db, version=str(db.SCHEMA_VERSION + 1), checksum=db.schema_checksum())
    with pytest.raises(RuntimeError, match="downgrading is not supported"):
        db.init_schema()


def test_ddl_change_without_a_version_bump_is_refused(scratch_db):
    db.init_schema()
    _stamp(scratch_db, version=str(db.SCHEMA_VERSION), checksum="different-checksum")
    with pytest.raises(RuntimeError, match="bump SCHEMA_VERSION"):
        db.init_schema()
