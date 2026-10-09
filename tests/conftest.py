"""Isolasi environment buat tests.

PENTING: env DISET di level modul, SEBELUM `engrix_router.core.config` ke-import. config.py
nangkep path pas module di-import (bukan pas fungsi dipanggil), jadi kalau
pengaturannya ditaruh di dalam fixture, test senyap nulis ke DB asli di data/.
Itu bug jenis "verified tapi salah objek" -- dihindari dari strukturnya.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))   # src-layout: paketnya ada di bawah src/

TEST_DATA = ROOT / "data" / "test"
os.environ["EROUTER_DATA_DIR"] = str(TEST_DATA)
os.environ["EROUTER_DB_PATH"] = str(TEST_DATA / "test.sqlite3")
os.environ["EROUTER_LOG_DIR"] = str(TEST_DATA / "logs")
os.environ["EROUTER_ADMIN_TOKEN"] = "unit-admin-token"
os.environ["EROUTER_LOG_FILE"] = "false"
os.environ["EROUTER_LOG_LEVEL"] = "error"
os.environ["EROUTER_REQUIRE_CLIENT_KEY"] = "true"

import pytest  # noqa: E402

from engrix_router.storage import sqlite as db
from engrix_router.core import config


@pytest.fixture(scope="session", autouse=True)
def _fresh_database():
    if TEST_DATA.exists():
        shutil.rmtree(TEST_DATA, ignore_errors=True)
    config.ensure_runtime_dirs()
    db.init_schema(force=True)
    yield
    shutil.rmtree(TEST_DATA, ignore_errors=True)


@pytest.fixture(autouse=True)
def _clean_rows():
    """Tiap tes mulai dari tabel kosong, jadi urutan file tes gak ngaruh."""
    yield
    with db.transaction() as conn:
        for table in ("request_stages", "requests", "usage_daily", "budgets", "model_locks",
                      "connection_health", "connections", "nodes", "api_keys", "proxy_pools",
                      "quota_snapshots", "settings", "provider_catalog"):
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 -- nama tabel hardcoded
