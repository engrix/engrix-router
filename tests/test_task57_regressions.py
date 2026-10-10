# Regression TASK-57: false-positive lock quota_daily (ZCode code 1005 padahal
# kuota akun masih ada -- akun zeno dikunci '*' 8106s satu detik setelah
# request pertamanya). Dua lapis perlindungan yang dikunci di sini:
# 1) register_error: meter vendor (quota_snapshots) yang masih menunjukkan
#    sisa >= floor menurunkan hukuman account-lock menjadi model-lock singkat.
# 2) reconcile_quota_locks: snapshot SEGAR yang bilang "masih ada quota"
#    melepas lock quota_daily yang sudah terlanjur terpasang (dan menurunkan
#    status unavailable -> cooling).

import pytest

from engrix_router.accounts import health
from engrix_router.core import errors
from engrix_router.storage.sqlite import execute, now_ms, query


@pytest.fixture()
def tmp_connection():
    cid = "test_conn_task57"
    execute(
        "INSERT OR REPLACE INTO connections(id, provider, node_id, auth_type, name, email,"
        " priority, is_active, cred_json, psd_json, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "zcode", None, "oauth", "tes-task57", None, 1, 1, "{}", "{}", now_ms(), now_ms()),
    )
    execute(
        "INSERT OR REPLACE INTO connection_health(connection_id, test_status, updated_at)"
        " VALUES(?,?,?)",
        (cid, health.STATUS_UNKNOWN, now_ms()),
    )
    return cid


def _meter(cid, *, remaining_pct, fetched_at, unlimited=0):
    execute(
        "INSERT INTO quota_snapshots(provider, connection_id, scope, used, total, remaining,"
        " remaining_pct, unlimited, reset_at, fetched_at, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("zcode", cid, "userQuota", 0, 3000000, int(3000000 * remaining_pct / 100),
         remaining_pct, unlimited, None, fetched_at, "{}"),
    )


def _quota_daily_error(cid, model="m"):
    return health.register_error(cid, model, errors.Classified(
        errors.CLASS_QUOTA_DAILY,
        errors.policy_of(errors.CLASS_QUOTA_DAILY),
        "exceed quota limit (tes)",
    ))


def test_register_error_menurunkan_hukuman_saat_meter_masih_ada_sisa(tmp_connection):
    cid = tmp_connection
    # meter segar: 100% tersisa, diambil 1 menit sebelum error
    _meter(cid, remaining_pct=100.0, fetched_at=now_ms() - 60_000)
    decision = _quota_daily_error(cid)
    assert decision["locked"] is True
    assert decision["scope"] == "model", decision
    locks = query("SELECT model FROM model_locks WHERE connection_id=?", (cid,))
    # bukan '*' -- akun tetap bisa dipakai model lain
    assert locks and all(row["model"] != "*" for row in locks), locks
    assert health.get(cid)["test_status"] == health.STATUS_COOLING


def test_register_error_tetap_mengunci_akun_saat_meter_kosong(tmp_connection):
    cid = tmp_connection
    _meter(cid, remaining_pct=0.0, fetched_at=now_ms() - 60_000)
    decision = _quota_daily_error(cid)
    assert decision["scope"] == "account", decision
    assert health.get(cid)["test_status"] == health.STATUS_UNAVAILABLE


def test_reconcile_melepas_lock_palsu_saat_snapshot_segar(tmp_connection):
    cid = tmp_connection
    # lock terlanjur terpasang (misal meter basi saat register_error)
    _meter(cid, remaining_pct=0.0, fetched_at=now_ms() - 60_000)
    _quota_daily_error(cid)
    assert health.get(cid)["test_status"] == health.STATUS_UNAVAILABLE
    # snapshot baru: vendor bilang masih 97% -- lock jadi false positive
    _meter(cid, remaining_pct=97.0, fetched_at=now_ms())
    released = health.reconcile_quota_locks(cid)
    assert released == 1, released
    assert query("SELECT * FROM model_locks WHERE connection_id=?", (cid,)) == []
    assert health.get(cid)["test_status"] == health.STATUS_COOLING
    assert health.is_available(cid, "m") is True


def test_reconcile_tidak_melepas_saat_kuota_benar_benar_habis(tmp_connection):
    cid = tmp_connection
    _meter(cid, remaining_pct=0.0, fetched_at=now_ms())
    # masukkan lock manual menyerupai hasil register_error tanpa meter
    execute(
        "INSERT INTO model_locks(connection_id, model, locked_until, reason, created_at)"
        " VALUES(?,?,?,?,?)",
        (cid, "*", now_ms() + 3_600_000, "quota_daily:policy:quota_window", now_ms()),
    )
    assert health.reconcile_quota_locks(cid) == 0
    assert query("SELECT * FROM model_locks WHERE connection_id=?", (cid,))


def test_reconcile_abaikan_snapshot_lebih_tua_dari_lock(tmp_connection):
    cid = tmp_connection
    # snapshot lama (sebelum error) bilang kuota penuh -- bukan bukti
    _meter(cid, remaining_pct=100.0, fetched_at=now_ms() - 3_600_000)
    execute(
        "INSERT INTO model_locks(connection_id, model, locked_until, reason, created_at)"
        " VALUES(?,?,?,?,?)",
        (cid, "*", now_ms() + 3_600_000, "quota_daily:policy:quota_window", now_ms()),
    )
    assert health.reconcile_quota_locks(cid) == 0
    assert query("SELECT * FROM model_locks WHERE connection_id=?", (cid,))
