# Regression TASK-45: tiga fix kritis yang sebelumnya bolong.
# 1) Reservation di-settle ke periode reserve, bukan "hari ini" --
#    refund lintas tengah malam UTC harus balik ke hari yang di-charge.
# 2) unavailable boleh pulih sendiri ke cooling kalau semua lock expired
#    -- dulu jebakan satu arah sampai admin re-test manual.
# 3) Skip reason tidak lagi menulis literal 'locked/status'.

import pytest

from engrix_router.accounts import health
from engrix_router.core import errors
from engrix_router.hooks import budget
from engrix_router.storage.sqlite import execute, now_ms, query_one


@pytest.fixture()
def tmp_connection():
    # # koneksi dummy + baris health-nya; dibersihkan _clean_rows conftest
    cid = "test_conn_task45"
    execute(
        "INSERT OR REPLACE INTO connections(id, provider, node_id, auth_type, name, email,"
        " priority, is_active, cred_json, psd_json, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "qoder", None, "oauth", "tes-task45", None, 1, 1, "{}", "{}", now_ms(), now_ms()),
    )
    execute(
        "INSERT OR REPLACE INTO connection_health(connection_id, test_status, updated_at)"
        " VALUES(?,?,?)",
        (cid, health.STATUS_UNKNOWN, now_ms()),
    )
    return cid


def test_settle_mengembalikan_ke_period_reserve_bukan_hari_ini():
    # # Simulasi reserve kemarin, settle hari ini: delta harus masuk ke
    # # periode KEMARIN (period_key yang tersimpan), bukan tanggal hari ini.
    old_day = "2000-01-01"
    res = budget.Reservation(scopes=[budget.SCOPE_GLOBAL], tokens=5000,
                             requests=1, period_key=old_day)
    budget.ensure_bucket(budget.SCOPE_GLOBAL, period=old_day)
    budget.settle(res, 0)  # # stream batal: semua 5000 kembali
    row = query_one("SELECT used_tokens FROM budgets WHERE period_key=? AND scope=?",
                    (old_day, budget.SCOPE_GLOBAL))
    assert row is not None and row["used_tokens"] == 0


def test_unavailable_pulih_sendiri_saat_lock_expired(tmp_connection):
    cid = tmp_connection
    # # daftarkan error quota_daily -> status unavailable + lock berwaktu
    health.register_error(cid, "m", errors.Classified(
        errors.CLASS_QUOTA_DAILY,
        errors.policy_of(errors.CLASS_QUOTA_DAILY),
        "quota habis (tes)",
    ))
    assert health.get(cid)["test_status"] == health.STATUS_UNAVAILABLE
    # # mundurkan semua lock ke masa lalu
    execute("UPDATE model_locks SET locked_until=? WHERE connection_id=?",
            (now_ms() - 1000, cid))
    # # sekarang harus available lagi -- self-heal jalan
    assert health.is_available(cid, "m") is True
    assert health.get(cid)["test_status"] == health.STATUS_COOLING


def test_skip_reason_tidak_menulis_literal_status(tmp_connection):
    from engrix_router.accounts import connections as conn_mod
    from engrix_router.core.types import ProviderDef
    cid = tmp_connection
    health.register_error(cid, "m", errors.Classified(
        errors.CLASS_QUOTA_DAILY,
        errors.policy_of(errors.CLASS_QUOTA_DAILY),
        "quota habis (tes)",
    ))
    definition = ProviderDef(id="qoder", category="agentic", transport="sse",
                             display_name="Qoder", models=[])
    _candidates, skipped = conn_mod.candidates(definition, "m")
    for item in skipped:
        assert "'status'" not in str(item.get("reason")), item
