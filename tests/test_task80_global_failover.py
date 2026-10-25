"""Kontrak failover GLOBAL: error upstream apa pun harus pindah akun.

Kenapa file ini ada: log produksi 2026-10-25 menunjukkan dua pola yang
bertentangan dengan tujuan router.

  1. `class=unknown` (464 baris, SEMUA berisi "transport error: ...") punya
     retry_internally=False, jadi satu ConnectError mematikan request di akun
     PERTAMA padahal ratusan akun lain sehat.
  2. `class=rate_limited` (532 baris, 429) juga retry_internally=False.

Yang diuji di sini bukan detail implementasi, tapi INVARIAN: satu request tidak
boleh menyerah selama masih ada akun lain yang belum dicoba -- kecuali error-nya
jelas bukan tentang akun (payload salah bentuk, model tidak ada, budget kita
sendiri, klien membatalkan, atau byte sudah terkirim ke klien).
"""
from __future__ import annotations

import pytest

from engrix_router.core import errors


# ── 1. setiap kelas yang berasal dari UPSTREAM harus boleh pindah akun ───────
UPSTREAM_CLASSES = (
    errors.CLASS_RATE_LIMIT,
    errors.CLASS_MODEL_CONCURRENCY,
    errors.CLASS_QUOTA_DAILY,
    errors.CLASS_NO_PLAN,
    errors.CLASS_PRICING_BLOCKED,
    errors.CLASS_QUEUE_THROTTLED,
    errors.CLASS_ANTI_ABUSE,
    errors.CLASS_CREDENTIAL_DEAD,
    errors.CLASS_UPSTREAM_UNAVAILABLE,
    errors.CLASS_UPSTREAM_TIMEOUT,
    errors.CLASS_UNKNOWN,
    errors.CLASS_CLIENT_BAD_REQUEST,
    errors.CLASS_MODEL_NOT_FOUND,
)


@pytest.mark.parametrize("error_class", UPSTREAM_CLASSES)
def test_upstream_class_allows_failover(error_class: str):
    """Setiap kelas yang datang dari upstream wajib boleh coba akun lain."""
    assert errors.policy_of(error_class).retry_internally is True, error_class


NOT_ABOUT_THE_ACCOUNT = (
    errors.CLASS_CLIENT_NO_KEY,
    errors.CLASS_CLIENT_BAD_KEY,
    errors.CLASS_BUDGET_EXCEEDED,
    errors.CLASS_ALL_LOCKED,
    errors.CLASS_ABORTED,
    errors.CLASS_STREAM_STALLED,
    errors.CLASS_PROTOCOL_DRIFT,
)


@pytest.mark.parametrize("error_class", NOT_ABOUT_THE_ACCOUNT)
def test_non_account_classes_stay_fail_fast(error_class: str):
    """Kelas yang tidak bisa dibantu akun lain HARUS tetap berhenti.

    Autentikasi ke gateway, budget kita sendiri, tidak ada kandidat, klien
    membatalkan, byte sudah terkirim, dan trip drift global -- mengganti akun
    di kasus ini bukan failover, itu menyembunyikan penyebabnya.
    """
    assert errors.policy_of(error_class).retry_internally is False, error_class


# ── 2. kelas yang salahnya di REQUEST dibatasi blast radius-nya ──────────────
def test_request_scoped_classes_are_the_payload_faults():
    assert errors.REQUEST_SCOPED_CLASSES == frozenset({
        errors.CLASS_CLIENT_BAD_REQUEST,
        errors.CLASS_MODEL_NOT_FOUND,
    })


def test_request_scoped_cap_setting_exists():
    from engrix_router.storage import settings

    default, typ, doc = settings.DEFAULTS["routing.request_scoped_attempts"]
    assert typ is int and default == 3
    assert "request" in doc.lower()


# ── 3. lock TIDAK ikut jadi agresif: akun sehat tidak boleh dihukum ─────────
def test_payload_fault_does_not_lock_the_account():
    """400/404 boleh pindah akun, tapi akunnya sendiri tetap sehat.

    Kalau kelas ini ikut mengunci akun, satu body salah bentuk akan menyapu
    seluruh fleet (self-DoS) -- justru itu yang dijaga health.lock_plain_4xx.
    """
    assert errors.policy_of(errors.CLASS_CLIENT_BAD_REQUEST).lock == "none"
    assert errors.policy_of(errors.CLASS_MODEL_NOT_FOUND).lock == "none"


def test_rate_limit_still_locks_with_backoff():
    """429 tetap menaikkan backoff -- rotasi bukan alasan memaafkan rate limit."""
    policy = errors.policy_of(errors.CLASS_RATE_LIMIT)
    assert policy.lock == "backoff"
    assert policy.retry_after_s == 15


# ── 4. bentuk pesan nyata dari log tetap terklasifikasi benar ────────────────
@pytest.mark.parametrize("text", [
    "transport error: ConnectError: [Errno 11001] getaddrinfo failed",
    "transport error: ReadError: ",
    "transport error: RemoteProtocolError: peer closed connection without sending complete message body",
    "transport error: ReadTimeout: ",
    "transport error: ConnectTimeout: ",
])
def test_production_transport_bodies_never_land_in_unknown(text: str):
    """Semua varian body 'transport error: ...' di log harus jadi kelas transient."""
    result = errors.classify(text=text)
    assert result.error_class in (errors.CLASS_UPSTREAM_UNAVAILABLE,
                                  errors.CLASS_UPSTREAM_TIMEOUT), text
    assert result.policy.retry_internally is True


def test_5xx_with_no_body_still_fails_over():
    result = errors.classify(status=503, text="")
    assert result.policy.retry_internally is True


def test_unmatched_status_fails_over():
    """Status aneh (510/511/507 dst) tidak boleh jadi jalan buntu."""
    for status in (507, 508, 510, 511):
        assert errors.classify(status=status, text="").policy.retry_internally is True, status


# ── 5. "sticky 1": default routing = round-robin, satu request per akun ─────
def test_default_routing_is_round_robin_with_sticky_one():
    from engrix_router.storage import settings

    assert settings.DEFAULTS["routing.strategy"][0] == "round-robin"
    assert settings.DEFAULTS["routing.sticky_round_robin_limit"][0] == 1


def test_round_robin_with_sticky_one_moves_after_a_single_use():
    """sticky_limit=1: akun yang barusan dipakai TIDAK boleh dipilih lagi."""
    from engrix_router.core.types import Candidate, Credentials, ProviderDef, TransportSpec
    from engrix_router.routing import selector

    definition = ProviderDef(id="t", category="apikey", transport=TransportSpec(base_url="https://x"))

    def candidate(cid: str, priority: int, last_used, count: int) -> Candidate:
        return Candidate(
            credentials=Credentials(connection_id=cid, provider="t", auth_type="apikey", name=cid, token="k"),
            provider_def=definition, priority=priority, test_status="active",
            last_used_ms=last_used, consecutive_use_count=count, rate_limited_until_ms=None,
        )

    fresh = candidate("fresh", 1, 900, 1)   # baru dipakai sekali
    old = candidate("old", 2, 100, 0)       # paling lama tidak dipakai
    ordered = selector.order([fresh, old], strategy=selector.STRATEGY_ROUND_ROBIN,
                             sticky_limit=1, ts=1000)
    assert ordered[0].credentials.connection_id == "old"


# ── 6. jeda sebelum failover harus nol ─────────────────────────────────────
def test_no_sleep_between_accounts():
    """Menunggu sebelum pindah akun hanya menambah latency tanpa manfaat."""
    from engrix_router.core.types import ProviderDef, TransportSpec
    from engrix_router.pipeline import runner

    definition = ProviderDef(id="t", category="apikey", transport=TransportSpec(base_url="https://x"))
    classified = errors.classify(status=503, text="upstream 503")
    assert runner._sleep_before_retry(classified, definition) == 0.0
