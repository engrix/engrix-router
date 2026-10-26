"""
SQLite access: connections, write transactions, schema bootstrap.

Design choices:
  - a connection per operation instead of manual pooling. SQLite + WAL makes a
    new connection cheap, and it removes the whole "connection shared across
    threads/loops" bug class.
  - every WRITE goes through transaction() under one global mutex. That is the
    counterpart of 9router's connection-selection mutex
    (sse/services/auth.js:10,36-38,227-229), which serialises
    read-pick-write-state so two requests cannot race on
    consecutive_use_count / backoff_level.
  - the schema comes only from src/engrix_router/schema.sql (fresh databases), and
    changing it for EXISTING databases means one entry in MIGRATIONS below. No ALTER
    scattered around the codebase.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engrix_router.core import config

_WRITE_LOCK = threading.Lock()
_SCHEMA_READY = False

# Koneksi persisten per thread, dipisah per (path, readonly).
#
# Kenapa ini bukan "pooling demi gaya": SQLite menjalankan checkpoint penuh dan
# MENGHAPUS file -wal setiap kali koneksi terakhir ditutup. Dengan satu koneksi
# per operasi, setiap operasi adalah "close terakhir", jadi setiap operasi bayar
# checkpoint. Terukur di mesin owner (probe.sqlite3, 60 putaran):
#
#   open + SELECT + close       6,232 ms
#   SELECT di koneksi persisten 0,017 ms
#   open + 1 UPDATE + close    19,433 ms
#   1 UPDATE di koneksi persisten 0,084 ms   -> 231x
#
# Thread-local, bukan satu koneksi global: objek sqlite3.Connection tidak boleh
# dipakai dua thread sekaligus, dan check_same_thread=False cuma mematikan
# pemeriksaannya, bukan bahayanya. Satu thread satu koneksi = aman tanpa lock
# tambahan di jalur baca.
_TLS = threading.local()


def _cache() -> dict[tuple[str, bool], sqlite3.Connection]:
    cache = getattr(_TLS, "conns", None)
    if cache is None:
        cache = {}
        _TLS.conns = cache
    return cache


# Nilai env saat modul di-import. Dipakai sebagai pembanding: kalau env sekarang
# BEDA dari baseline, berarti ada yang mengubahnya saat runtime (monkeypatch.setenv)
# dan override itu harus dihormati -- lalu otomatis reversibel begitu fixture-nya
# mengembalikan nilai lama.
_ENV_DB_BASELINE = os.environ.get("EROUTER_DB_PATH")


def db_path() -> Path:
    """Resolve the database path at CALL time, not at import time.

    The suite switches database two different ways and both must work:

      * `monkeypatch.setenv("EROUTER_DB_PATH", tmp)`  -- env differs from the
        baseline captured at import, so it wins. Reversible on teardown.
      * `monkeypatch.setattr(config, "DB_PATH", tmp)` -- env is untouched, so
        config.DB_PATH wins. Also reversible on teardown.

    Capturing the path at import would make BOTH silently keep writing to the
    shared test database while the test still passes -- the failure mode the
    conftest docstring warns about.
    """
    current_env = os.environ.get("EROUTER_DB_PATH")
    if current_env != _ENV_DB_BASELINE:
        return Path(current_env).resolve() if current_env else config.DB_PATH
    return config.DB_PATH


def _persistent(*, readonly: bool) -> sqlite3.Connection:
    """This thread's reusable connection for the current DB path.

    Connections to a path that is no longer current are closed and dropped first:
    a test that repoints the database then deletes the old file would otherwise
    hit WinError 32 on Windows, because the handle is still open.
    """
    current = str(db_path())
    cache = _cache()
    for key in [k for k in cache if k[0] != current]:
        try:
            cache.pop(key).close()
        except sqlite3.Error:
            pass
    key = (current, readonly)
    conn = cache.get(key)
    if conn is not None:
        return conn
    conn = connect(readonly=readonly)
    cache[key] = conn
    return conn


def close_all() -> int:
    """Close every cached connection on every thread that has one.

    Only the CALLING thread's connections can be closed safely; the others are
    closed by their own thread when it next resets. Returns how many this thread
    closed. Tests and shutdown use it so Windows does not keep the file locked.
    """
    cache = _cache()
    closed = 0
    for conn in list(cache.values()):
        try:
            conn.close()
            closed += 1
        except sqlite3.Error:
            pass
    cache.clear()
    return closed


def reset_for_tests(path: str | None = None) -> None:
    """Drop this thread's connections and forget the schema bootstrap.

    Deliberately does NOT mutate config.DB_PATH: doing that made the switch
    permanent, so the test that asked for it left every later test pointed at a
    tmp file that its own teardown had already deleted. Switching databases is
    the env var's job now (see db_path), which the fixture can undo. `path` stays
    in the signature only so existing callers keep working.
    """
    close_all()
    global _SCHEMA_READY
    _SCHEMA_READY = False
    # Pemanggil yang pindah database ingin database yang BISA DIPAKAI, bukan file
    # kosong: tanpa ini query pertama gagal "no such table".
    init_schema(force=True)

SCHEMA_VERSION = 3

# Satu-satunya tempat ALTER ada. Aturan: ubah DDL = naikkin SCHEMA_VERSION + nambah
# entri di sini, kalau gak: DB lama gak pernah nyusul dan perubahannya senyap.
# Tiap langkah harus aman dijalankan dua kali -- _migrate nge-skip kolom yang udah ada.
MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE requests ADD COLUMN credits NUMERIC",
        "ALTER TABLE requests ADD COLUMN credits_original NUMERIC",
    ],
    3: [
        # Snapshot terbaru per koneksi tanpa full scan (lihat catatan di schema.sql):
        # query banner kuota dulu 19,98s dan ngeblok event loop. Aman dijalankan
        # berulang karena IF NOT EXISTS.
        "CREATE INDEX IF NOT EXISTS idx_quota_conn ON quota_snapshots(connection_id, fetched_at DESC)",
    ],
}


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_key_utc(epoch_ms: int | None = None) -> str:
    """Daily bucket key. UTC, not local: the vendor's daily reset (code 110) follows the UTC calendar."""
    moment = now_ms() if epoch_ms is None else int(epoch_ms)
    return datetime.fromtimestamp(moment / 1000, timezone.utc).strftime("%Y-%m-%d")


def connect(*, readonly: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if readonly:
        conn.execute("PRAGMA query_only = ON")
    else:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def session(*, readonly: bool = False):
    """`with session() as db:` - commits if the block succeeded, rolls back on raise.

    The connection is the thread's persistent one, NOT a fresh one: closing after
    every read forces SQLite to checkpoint the whole WAL and delete the -wal file.
    Measured 6.232 ms per open+SELECT+close against 0.017 ms on a persistent
    connection. The block still commits/rolls back exactly as before.
    """
    conn = _persistent(readonly=readonly)
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise


@contextmanager
def transaction():
    """Write transaction under the global mutex. Never hold it across slow work (HTTP/streaming); only for DB steps that must be atomic.

    The mutex is still the thing that makes read-pick-write-state race-free; what
    changed is only that the connection is reused instead of reopened, which
    removes the per-transaction checkpoint (19.433 ms -> 0.084 ms measured).
    """
    with _WRITE_LOCK:
        conn = _persistent(readonly=False)
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def query(sql: str, params: Sequence[Any] = (), *, readonly: bool = True) -> list[sqlite3.Row]:
    with session(readonly=readonly) as db:
        return list(db.execute(sql, params).fetchall())


def query_one(sql: str, params: Sequence[Any] = (), *, readonly: bool = True) -> sqlite3.Row | None:
    rows = query(sql, params, readonly=readonly)
    return rows[0] if rows else None


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """One write statement outside a multi-step transaction."""
    with transaction() as db:
        return db.execute(sql, params).rowcount


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def schema_checksum() -> str:
    return hashlib.sha256(config.SCHEMA_PATH.read_bytes()).hexdigest()[:16]


def _migrate(db: sqlite3.Connection, from_version: int) -> list[str]:
    """Apply MIGRATIONS newer than `from_version`, in order, skipping steps already done."""
    applied: list[str] = []
    for version in sorted(MIGRATIONS):
        if version <= from_version or version > SCHEMA_VERSION:
            continue
        for statement in MIGRATIONS[version]:
            table, column = _alter_targets(statement)
            if column is not None and column in {row[1] for row in
                                                 db.execute(f"PRAGMA table_info({table})")}:
                continue  # langkah ini udah pernah jalan di DB-nya
            db.execute(statement)
            applied.append(statement)
    return applied


def _alter_targets(statement: str) -> tuple[str, str | None]:
    """(table, new_column) from an `ALTER TABLE x ADD COLUMN y ...`, else (table, None)."""
    parts = statement.split()
    if parts[0].upper() != "ALTER" or len(parts) < 2:
        return ("", None)
    column = None
    if "COLUMN" in (p.upper() for p in parts):
        column = parts[parts.index(next(p for p in parts if p.upper() == "COLUMN")) + 1]
    return parts[2], column


def init_schema(*, force: bool = False) -> dict[str, Any]:
    """
    Apply schema.sql, then migrate an older database up to SCHEMA_VERSION.

    A DDL change is only allowed as a version bump plus a MIGRATIONS entry -- the
    baseline file describes NEW databases, migrations describe existing ones. That
    split is what makes `--force` honest: it can re-stamp a checksum, it can never
    pretend a missing column exists.
    """
    global _SCHEMA_READY
    config.ensure_runtime_dirs()
    sql = config.SCHEMA_PATH.read_text(encoding="utf-8")
    applied: list[str] = []
    with transaction() as db:
        db.executescript(sql)
        stored_version = db.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        existing = db.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_checksum'"
        ).fetchone()
        checksum = schema_checksum()
        version = int(stored_version["value"]) if stored_version else SCHEMA_VERSION
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"the database is at schema v{version} but this build knows v{SCHEMA_VERSION} -- "
                "downgrading is not supported"
            )
        if existing and existing["value"] != checksum and version == SCHEMA_VERSION and not force:
            raise RuntimeError(
                f"schema in the DB ({existing['value']}) differs from schema.sql ({checksum}) "
                f"at the same version v{version}. Either bump SCHEMA_VERSION and add a "
                "MIGRATIONS entry, or (comment-only change) run with force."
            )
        applied = _migrate(db, version)
        db.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_checksum', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (checksum,),
        )
        db.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        db.execute(
            "INSERT INTO schema_meta(key, value) VALUES('bootstrapped_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (now_iso(),),
        )
    _SCHEMA_READY = True
    return {"checksum": checksum, "tables": table_names(), "path": str(config.DB_PATH),
            "version": SCHEMA_VERSION, "migrations_applied": applied}


def table_names() -> list[str]:
    with session(readonly=True) as db:
        return [
            row["name"]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]


def ensure_ready() -> None:
    """Called by startup/tests: create the schema once per process."""
    if not _SCHEMA_READY:
        init_schema()


def count(table: str) -> int:
    if table not in KNOWN_TABLES:
        raise ValueError(f"unknown table: {table}")
    row = query_one(f"SELECT COUNT(*) AS n FROM {table}")  # noqa: S608 -- nama dari whitelist
    return int(row["n"]) if row else 0


KNOWN_TABLES = frozenset(
    {
        "api_keys",
        "nodes",
        "connections",
        "connection_health",
        "model_locks",
        "provider_catalog",
        "proxy_pools",
        "requests",
        "request_stages",
        "usage_daily",
        "quota_snapshots",
        "budgets",
        "settings",
        "schema_meta",
    }
)
