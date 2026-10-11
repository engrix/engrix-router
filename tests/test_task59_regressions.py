# Regression TASK-59: eksekusi temuan audit TASK-58 yang terbukti di HEAD.
# Yang dikunci di sini (temuan #6 usage_daily.total dan #7 duplikat _num_or_none
# DITOLAK saat re-verifikasi -- schema usage_daily memang tanpa kolom total dan
# zcode.py cuma punya satu definisi _num_or_none):
#   #1 register_success: sukses hanya mengangkat lock '*' + lock model yang
#      berhasil + lock expired; lock live model lain di koneksi itu dan di
#      koneksi lain tetap hidup.
#   #2/#8 sticky counter: +1 hanya saat akun YANG SAMA dipilih berulang
#      (mark_selected), reset saat pindah; register_success tidak menyentuhnya.
#   #3 sentinel vendor (tahun 9999) dari scope unlimited / reset meleset tidak
#      bisa lagi melahirkan lock quota_window multi-tahun.
#   #4 sweeper in_flight: mayat tua jadi aborted, request segar tak tersentuh,
#      idempotent, guard stale_ms=0 mematikan.
#   #5 register_defaults benar-benar menginvalidasi cache settings.

from types import SimpleNamespace

import pytest

from engrix_router.accounts import health
from engrix_router.core import errors
from engrix_router.core.types import Credentials
from engrix_router.routing import selector
from engrix_router.storage import settings
from engrix_router.storage.sqlite import execute, now_ms, query, query_one
from engrix_router.subscribers import trace

SENTINEL_9999 = 253402214400000  # tahun 9999, dikirim vendor utk scope unlimited


# ── helpers ──────────────────────────────────────────────────────────────────

def _conn(cid: str) -> str:
    execute(
        "INSERT OR REPLACE INTO connections(id, provider, node_id, auth_type, name, email,"
        " priority, is_active, cred_json, psd_json, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, "zcode", None, "oauth", cid, None, 1, 1, "{}", "{}", now_ms(), now_ms()),
    )
    execute(
        "INSERT OR REPLACE INTO connection_health(connection_id, test_status, updated_at)"
        " VALUES(?,?,?)",
        (cid, health.STATUS_UNKNOWN, now_ms()),
    )
    return cid


def _lock(cid: str, model: str, ttl_ms: int, reason: str = "tes") -> None:
    execute(
        "INSERT INTO model_locks(connection_id, model, locked_until, reason, created_at)"
        " VALUES(?,?,?,?,?)"
        " ON CONFLICT(connection_id, model) DO UPDATE SET locked_until=excluded.locked_until",
        (cid, model, now_ms() + ttl_ms, reason, now_ms()),
    )


def _locks(cid: str) -> dict[str, int]:
    return {row["model"]: int(row["locked_until"])
            for row in query("SELECT model, locked_until FROM model_locks WHERE connection_id=?", (cid,))}


def _cand(cid: str) -> SimpleNamespace:
    creds = Credentials(connection_id=cid, provider="zcode", auth_type="oauth",
                        name=cid, token="token-tes")
    return SimpleNamespace(credentials=creds)


def _quota_daily_error(cid: str, model: str = "m") -> dict:
    return health.register_error(cid, model, errors.Classified(
        errors.CLASS_QUOTA_DAILY,
        errors.policy_of(errors.CLASS_QUOTA_DAILY),
        "exceed quota limit (tes)",
    ))


def _snap(cid: str, *, remaining_pct: float, reset_at: int | None,
          unlimited: int = 0, fetched_at: int | None = None) -> None:
    execute(
        "INSERT INTO quota_snapshots(provider, connection_id, scope, used, total, remaining,"
        " remaining_pct, unlimited, reset_at, fetched_at, raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("zcode", cid, "userQuota", 0, 3_000_000, 0, remaining_pct, unlimited,
         reset_at, fetched_at if fetched_at is not None else now_ms(), "{}"),
    )


def _req(rid: str, ts: int, status: str = "in_flight") -> None:
    execute(
        "INSERT INTO requests(id, ts, endpoint, kind, status, budget_lane) VALUES(?,?,?,?,?,?)",
        (rid, ts, "/v1/chat/completions", "chat", status, "interactive"),
    )


# ── #1 register_success: scope pengangkatan lock ────────────────────────────

def test_register_success_normal_mengangkat_wildcard_dan_model_sukses():
    a, b = _conn("t59-ra"), _conn("t59-rb")
    _lock(a, "*", 3_600_000)
    _lock(a, "m1", 3_600_000)
    _lock(b, "m2", 3_600_000)  # koneksi lain -- sama sekali tidak boleh tersentuh
    health.register_success(a, "m1")
    assert _locks(a) == {}, _locks(a)
    assert set(_locks(b)) == {"m2"}


def test_register_success_hard_lock_live_model_lain_tetap_hidup():
    a = _conn("t59-rc")
    _lock(a, "*", 3_600_000)
    _lock(a, "m1", 3_600_000)   # lock live model LAIN dari sukses ini
    _lock(a, "m3", -1_000)      # expired
    health.register_success(a)  # tanpa info model
    assert set(_locks(a)) == {"m1"}, _locks(a)


def test_register_success_extreme_menyapu_lock_expired_tanpa_model():
    a = _conn("t59-rd")
    for i in range(10):
        _lock(a, f"m{i}", -1_000)  # semua expired
    health.register_success(a)
    assert _locks(a) == {}


def test_register_success_tidak_menaikkan_sticky_counter():
    a = _conn("t59-re")
    execute("UPDATE connection_health SET consecutive_use_count=7 WHERE connection_id=?", (a,))
    health.register_success(a, "m1")
    assert health.get(a)["consecutive_use_count"] == 7


# ── #2/#8 sticky counter: semantik 9router ──────────────────────────────────

def test_mark_selected_naik_hanya_saat_akun_sama_dipilih_ulang(monkeypatch):
    clock = {"t": 1_000_000}

    def fake_now():
        clock["t"] += 10
        return clock["t"]

    monkeypatch.setattr(selector, "now_ms", fake_now)
    a, b = _conn("t59-sa"), _conn("t59-sb")
    selector.mark_selected(_cand(a))
    selector.mark_selected(_cand(a))
    assert health.get(a)["consecutive_use_count"] == 2
    selector.mark_selected(_cand(b))
    assert health.get(b)["consecutive_use_count"] == 1
    assert health.get(a)["consecutive_use_count"] == 2
    selector.mark_selected(_cand(a))  # pindah kembali -> reset ke 1
    assert health.get(a)["consecutive_use_count"] == 1


def test_sticky_counter_legacy_112_direset_tidak_dilanjutkan(monkeypatch):
    clock = {"t": 2_000_000}

    def fake_now():
        clock["t"] += 10
        return clock["t"]

    monkeypatch.setattr(selector, "now_ms", fake_now)
    a = _conn("t59-sc")
    execute("UPDATE connection_health SET consecutive_use_count=112 WHERE connection_id=?", (a,))
    selector.mark_selected(_cand(a))
    assert health.get(a)["consecutive_use_count"] == 1


def test_order_round_robin_nempel_lalu_pindah_saat_lewat_limit():
    def cand(cid, cuc, last):
        return SimpleNamespace(credentials=SimpleNamespace(connection_id=cid),
                               priority=1, last_used_ms=last, consecutive_use_count=cuc)

    fresh = cand("a", cuc=2, last=2_000)
    stale = cand("b", cuc=1, last=1_000)
    ordered = selector.order([stale, fresh], strategy=selector.STRATEGY_ROUND_ROBIN, sticky_limit=3)
    assert ordered[0].credentials.connection_id == "a"
    over = cand("a", cuc=3, last=3_000)
    ordered = selector.order([over, stale], strategy=selector.STRATEGY_ROUND_ROBIN, sticky_limit=3)
    assert ordered[0].credentials.connection_id == "b"


# ── #3 sentinel reset_at ─────────────────────────────────────────────────────

def test_quota_window_normal_mengikuti_reset_vendor_terukur():
    cid = _conn("t59-qa")
    _snap(cid, remaining_pct=0.0, reset_at=now_ms() + 3 * 3_600_000)
    decision = _quota_daily_error(cid)
    assert decision["locked"] is True and decision["scope"] == "account", decision
    assert 2 * 3_600_000 < decision["lock_ms"] < 4 * 3_600_000, decision
    assert _locks(cid) == {"*": int(query_one(
        "SELECT locked_until FROM model_locks WHERE connection_id=?", (cid,))["locked_until"])}


def test_quota_window_hard_sentinel_unlimited_tidak_ikut_max():
    cid = _conn("t59-qb")
    _snap(cid, remaining_pct=100.0, reset_at=SENTINEL_9999, unlimited=1)
    _snap(cid, remaining_pct=0.0, reset_at=now_ms() + 3 * 3_600_000)
    decision = _quota_daily_error(cid)
    assert decision["scope"] == "account", decision
    assert decision["lock_ms"] < 6 * 3_600_000, decision  # bukan ~8000 tahun


def test_quota_window_extreme_sentinel_di_scope_terukur_jatuh_ke_fallback():
    cid = _conn("t59-qc")
    _snap(cid, remaining_pct=0.0, reset_at=SENTINEL_9999)
    decision = _quota_daily_error(cid)
    assert decision["scope"] == "account", decision
    assert decision["lock_ms"] <= 3_600_000, decision  # fallback cooldown, bukan sentinel


# ── #4 sweeper in_flight ─────────────────────────────────────────────────────

def test_sweep_normal_hanya_menyentuh_mayat_tua():
    _req("t59-w1", now_ms() - 2 * 3_600_000)             # mayat
    _req("t59-w2", now_ms() - 60_000)                    # segar
    _req("t59-w3", now_ms() - 2 * 3_600_000, status="ok")
    assert trace.sweep_in_flight_once() == 1
    assert query_one("SELECT status FROM requests WHERE id=?", ("t59-w1",))["status"] == "aborted"
    assert query_one("SELECT status FROM requests WHERE id=?", ("t59-w2",))["status"] == "in_flight"
    assert query_one("SELECT status FROM requests WHERE id=?", ("t59-w3",))["status"] == "ok"
    assert query_one("SELECT finished_at FROM requests WHERE id=?", ("t59-w1",))["finished_at"] is not None


def test_sweep_hard_idempotent_dan_guard_nol():
    _req("t59-w4", now_ms() - 5 * 3_600_000)
    assert trace.sweep_in_flight_once() == 1
    assert trace.sweep_in_flight_once() == 0
    assert trace.sweep_in_flight_once(stale_ms=0) == 0


def test_sweep_extreme_seratus_mayat_sekali_jalan():
    for i in range(100):
        _req(f"t59-x{i}", now_ms() - 2 * 3_600_000 - i)
    assert trace.sweep_in_flight_once() == 100
    assert query_one("SELECT COUNT(*) AS n FROM requests WHERE status='in_flight'")["n"] == 0


# ── #5 register_defaults invalidasi cache ────────────────────────────────────

def test_register_defaults_terlihat_di_all_settings_tanpa_restart():
    settings.register_defaults({"t59.a": (1, int, "a")}, owner="t59")
    assert settings.all_settings()["t59.a"] == 1  # cache terbangun di sini
    settings.register_defaults({"t59.b": (2, int, "b")}, owner="t59")
    assert settings.all_settings()["t59.b"] == 2  # tanpa fix: cache basi tanpa kunci ini


def test_register_defaults_extreme_idempotent_beda_default_prefix():
    settings.register_defaults({"t59.c": (3, int, "c")}, owner="t59")
    settings.register_defaults({"t59.c": (3, int, "c")}, owner="t59")  # sama -> boleh
    with pytest.raises(ValueError):
        settings.register_defaults({"t59.c": (4, int, "c")}, owner="t59")
    with pytest.raises(ValueError):
        settings.register_defaults({"salah.t59": (1, int, "x")}, owner="t59")
