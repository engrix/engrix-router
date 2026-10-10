"""Kontrak inti router — bagian yang kalau salah bikin engrix-agent nangis.

Dites di sini (bukan cuma di scripts/verify_slice.py, yang jalurnya end-to-end):
  1. usage: idempoten, max-merge, cache clamp, TANPA +2000 buffer ala 9router
  2. errors: peta status yg ngubah perilaku engrix (403/401 = fatal + bench 24 jam)
  3. routing: fill-first vs sticky round-robin vs least-recent
  4. identity/api_keys: hash-only, prefix, verify
  5. url_guard: SSRF + proxy userinfo
  6. pricing: prompt cache-inclusive
  7. trace: truncation yg ada penanda, bukan dipotong diem-diem
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from engrix_router.subscribers import pricing, usage
from engrix_router.core import errors
from engrix_router.core.types import Candidate, Credentials, ProviderDef, TransportSpec


# ── 1. usage ─────────────────────────────────────────────────────────────────
def test_extract_dari_chunk_openai():
    found = usage.extract({"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
                                    "completion_tokens_details": {"reasoning_tokens": 3},
                                    "prompt_tokens_details": {"cached_tokens": 4}}})
    assert found == {"prompt": 11, "completion": 7, "reasoning": 3, "cached": 4,
                     "cache_creation": 0, "total": 18}


def test_canonicalize_idempoten_dan_terima_dua_bentuk():
    raw = {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25}
    once = usage.canonicalize(raw)
    twice = usage.canonicalize(once)
    assert once == twice
    assert once["prompt"] == 20 and once["total"] == 25


def test_merge_pakai_max_bukan_sum():
    a = usage.canonicalize({"prompt": 100, "completion": 10, "reasoning": 0, "cached": 0,
                            "cache_creation": 0, "total": 110})
    b = usage.canonicalize({"prompt": 100, "completion": 40, "reasoning": 12, "cached": 8,
                            "cache_creation": 0, "total": 140})
    merged = usage.merge(a, b)
    assert merged["completion"] == 40 and merged["reasoning"] == 12 and merged["total"] == 140


def test_usage_tidak_pernah_dibengkakkan_2000():
    """Regresi khusus: 9router nambah BUFFER_TOKENS=2000 ke keluaran klien
    (open-sse/utils/usageTracking.js:21, dipakai di stream.js:239/399), dan
    engrix cuma baca total_tokens -> ledger dia +2000 per call."""
    out = usage.as_client_usage_payload({"prompt": 10, "completion": 4, "total": 14})
    assert out == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def test_cached_diclamp_ke_prompt():
    out = usage.canonicalize({"prompt_tokens": 5, "completion_tokens": 1, "cached_tokens": 99,
                              "total_tokens": 6})
    assert out["cached"] == 5


# ── 2. errors / kontrak status ───────────────────────────────────────────────
def test_kuota_harian_jadi_503_bukan_403():
    c = errors.classify(status=403, text='{"code":"110","message":"Billing daily count exceeded"}')
    assert c.error_class == errors.CLASS_QUOTA_DAILY
    assert c.client_status == 503          # 403 = engrix bench key 86400s (key_pool.py:78-85)
    assert c.reset_at is not None and c.reset_at.tzinfo == timezone.utc
    assert c.retry_after_s and c.retry_after_s > 0


def test_qoder_queue_throttle_inner_code_wins_over_status_echo():
    """Insiden 2026-10-09 21:58: vendor bungkus 10605 dalam 403 bertingkat --
    `"code":"403"` di luar nyaris menenggelamkan kode yang punya policy sendiri,
    jadi terklasifikasi credential_dead (bench 120 s) padahal vendor cuma bilang
    "antrian model gratis, coba lagi 30 dtk"."""
    inner = json.dumps({"isQueued": True, "modelKey": "qfmodel",
                        "retryAfterSeconds": 30, "serviceAvailable": True})
    mid = json.dumps({"code": "10605", "message": inner})
    body = json.dumps({"body": json.dumps({"code": "403", "message": mid})})
    c = errors.classify(status=403, text=body)
    assert c.error_class == errors.CLASS_QUEUE_THROTTLED
    assert c.vendor_code == "10605"
    assert c.retry_after_s == 30          # retryAfterSeconds vendor, bukan default 8
    assert c.client_status == 429         # 429 + Retry-After, BUKAN credential_dead


def test_token_mati_jadi_503_bukan_401():
    c = errors.classify(status=401, text="Login expired")
    assert c.error_class == errors.CLASS_CREDENTIAL_DEAD
    assert c.client_status == 503          # 401 juga bench 24 jam di engrix
    assert c.policy.lock == "auth"


def test_signature_invalid_diklasifikasi_drift():
    c = errors.classify(status=403, text="Signature invalid")
    assert c.error_class == errors.CLASS_PROTOCOL_DRIFT and c.client_status == 502
    d = errors.classify(status=403, text='{"code":"103","message":"Duplicate request"}')
    assert d.error_class == errors.CLASS_PROTOCOL_DRIFT


def test_400_upstream_tidak_ngunci_akun():
    c = errors.classify(status=400, text="invalid_request_error")
    assert c.error_class == errors.CLASS_CLIENT_BAD_REQUEST
    assert c.policy.lock == "none" and not c.policy.mark_connection


def test_budget_ke_per_class_bukan_kelas_rate_limit():
    """Regresi: dulu lewat classify(status=429) -> ke-map jadi rate_limited,
    jadi log/dashboard gak bisa bedain limit vendor sama keputusan budget kita."""
    c = errors.for_class(errors.CLASS_BUDGET_EXCEEDED, "budget harian tercapai", retry_after_s=3600)
    assert c.error_class == errors.CLASS_BUDGET_EXCEEDED and c.client_status == 429
    body = c.error_body(15)
    assert body["error"]["code"] == "budget_exceeded"
    assert body["error"]["retry_after"] == 3600


def test_pricings_blocked_kelas_model_lock():
    c = errors.classify(status=403, text='{"code":"112","message":"{\"pricingUrl\":\"https://qoder.com/pricing\"}"}')
    assert c.error_class == errors.CLASS_PRICING_BLOCKED
    assert c.policy.lock == "model" and not c.policy.retry_internally


# ── 3. routing ───────────────────────────────────────────────────────────────
def _candidate(cid: str, priority: int, last_used=None, count: int = 0) -> Candidate:
    definition = ProviderDef(id="t", category="apikey", transport=TransportSpec(base_url="https://x"))
    creds = Credentials(connection_id=cid, provider="t", auth_type="apikey", name=cid, token="k")
    return Candidate(credentials=creds, provider_def=definition, priority=priority,
                     test_status="active", last_used_ms=last_used, consecutive_use_count=count,
                     rate_limited_until_ms=None)


def test_fill_first_selalu_prioritas_terkecil():
    from engrix_router.routing import selector

    cands = [_candidate("c2", 2), _candidate("c1", 1), _candidate("c3", 3)]
    ordered = selector.order(cands, strategy=selector.STRATEGY_FILL_FIRST, sticky_limit=3)
    assert [c.credentials.connection_id for c in ordered][:1] == ["c1"]


def test_round_robin_sticky_nempel_sampai_limit():
    from engrix_router.routing import selector

    fresh = _candidate("a", 1, last_used=100, count=1)
    old = _candidate("b", 2, last_used=50, count=0)
    # count < sticky_limit -> tetap di yang paling baru dipakai
    assert selector.order([old, fresh], strategy=selector.STRATEGY_ROUND_ROBIN, sticky_limit=3)[0] is fresh
    # count >= limit -> pindah ke yang paling basi
    over = _candidate("a", 1, last_used=100, count=3)
    assert selector.order([over, old], strategy=selector.STRATEGY_ROUND_ROBIN, sticky_limit=3)[0] is old


def test_least_recent_dahulukan_yang_belum_pernah_pakai():
    from engrix_router.routing import selector

    used = _candidate("u", 1, last_used=999)
    never = _candidate("n", 5, last_used=None)
    ordered = selector.order([used, never], strategy=selector.STRATEGY_LEAST_RECENT, sticky_limit=1)
    assert ordered[0].credentials.connection_id == "n"


# ── 4. client keys ───────────────────────────────────────────────────────────
def test_api_key_disimpan_sebagai_hash():
    from engrix_router.storage.sqlite import query
    from engrix_router.identity import api_keys

    created = api_keys.create_key("engrix-agent")
    assert created["key"].startswith("sk-er-")
    rows = query("SELECT key_hash, key_prefix FROM api_keys")
    assert created["key"] not in [r["key_hash"] for r in rows]
    assert all(len(r["key_hash"]) == 64 for r in rows)
    assert api_keys.verify(created["key"])["name"] == "engrix-agent"
    assert api_keys.verify("sk-er-palsu") is None
    # key lama tetap dikenali walau label diganti: verifikasi pakai hash penuh


def test_extract_client_key_ikut_urutan_9router():
    from engrix_router.identity import api_keys

    assert api_keys.extract_client_key(authorization="Bearer abc") == "abc"
    assert api_keys.extract_client_key(x_api_key="k2") == "k2"
    assert api_keys.extract_client_key(query_key="q4") == "q4"
    assert api_keys.extract_client_key() is None


# ── 5. url guard ─────────────────────────────────────────────────────────────
def test_url_guard_tolak_internal_dan_terima_publik():
    from engrix_router.transport import url_guard

    for bad in ("http://127.0.0.1:1234/v1", "http://169.254.169.254/latest/meta-data",
                "http://10.0.0.5/v1", "http://localhost:20128/v1"):
        ok, _ = url_guard.is_public_target(bad)
        assert ok is False, bad
    ok, why = url_guard.is_public_target("https://api.groq.com/openai/v1")
    assert ok is True, why
    # normalize cuma ngupas endpoint yang nempel di path, prefix /v1 dibiarin
    assert url_guard.normalize_base_url("https://x.test/v1/chat/completions") == "https://x.test/v1"
    assert url_guard.normalize_base_url("https://x.test/v1/models") == "https://x.test/v1"


def test_cost_for_memakai_usage_dan_tanpa_tabel_balik_nol():
    usage_ok = {"prompt": 1000, "completion": 1000, "cached": 0, "cache_creation": 0, "reasoning": 0}
    cost, known = pricing.cost_for("openai", "gpt-4o-mini", usage_ok)
    assert known is True and cost > 0
    cost_unknown, known_unknown = pricing.cost_for("node-x", "model-aneh", usage_ok)
    assert cost_unknown == 0.0 and known_unknown is False


def test_proxy_url_tidak_boleh_membawa_userinfo():
    from engrix_router.transport import proxy
    try:
        proxy._validate_shape("http://user:pass@172.17.0.1:7897")
        raise AssertionError("seharusnya ditolak")
    except ValueError as exc:
        assert "userinfo" in str(exc)
    # IP privat untuk PROXY itu wajar (biasanya di host/docker network yang sama)
    assert proxy._validate_shape("http://172.17.0.1:7897").startswith("http://172.17.0.1")
    # tapi scheme aneh tetap ditolak
    try:
        proxy._validate_shape("ftp://x.test:21")
        raise AssertionError("seharusnya ditolak")
    except ValueError:
        pass


# ── 6. pricing ───────────────────────────────────────────────────────────────
def test_prompt_dianggap_termasuk_cached():
    rates = {"input": 2.0, "output": 8.0, "cached": 0.5, "reasoning": None, "cache_creation": None}
    bill = pricing.calculate({"prompt": 1000, "completion": 500, "cached": 600,
                              "cache_creation": 0, "reasoning": 0}, rates)
    assert round(bill, 8) == round((400 * 2 + 600 * 0.5 + 500 * 8) / 1e6, 8)




def test_namespace_gratis_menang_dari_canonical():
    assert pricing.resolve("qoder", "qfmodel") == pricing.ZERO


# ── 7. trace ────────────────────────────────────────────────────────────────
def test_stage_trace_dipotong_dengan_penanda():
    """Payload kegedean gak boleh dipotong diem-diem: harus ada penanda + ukuran
    asli (pola `_truncated/_originalSize` dari requestDetailsRepo.js:80-86)."""
    from engrix_router.subscribers import trace as trace_store

    recorder = trace_store.begin("/v1/chat/completions")
    recorder.bind_target(provider="unit", model="m")
    recorder.stage(trace_store.STEP_PROVIDER_OUT, {"blob": "x" * 200_000}, direction="out")
    item = trace_store.get(recorder.request_id)
    stage = next(s for s in item["stages"] if s["name"] == "provider_out")
    assert stage["truncated"] == 1
    assert stage["original_bytes"] > len(stage["payload_json"])


def test_finish_mencatat_usage_dan_rollup_harian():
    from engrix_router.storage.sqlite import query_one
    from engrix_router.subscribers import trace as trace_store

    recorder = trace_store.begin("/v1/chat/completions")
    recorder.bind_target(provider="unit", model="m")
    result = recorder.finish(status=trace_store.STATUS_OK, http_out=200,
                             usage={"prompt": 10, "completion": 4, "total": 14})
    assert result["usage"]["total"] == 14
    row = query_one("SELECT prompt, completion, cached, requests FROM usage_daily WHERE provider='unit'")
    assert (row["prompt"], row["completion"], row["requests"]) == (10, 4, 1)


# ── 8. wire layer: encoding + signature hooks ────────────────────────────────
def test_wire_hook_encode_body_dan_sign_request():
    """Provider dengan body obfuscation + signing di atas byte final harus bisa
    dipasang tanpa ngubah core. Ini jalur yang dibutuhin port qoder/COSY nanti
    (encoding WAF-bypass + Cosy-Bodyhash/Bodylength + replay guard)."""
    from engrix_router.providers.base import BaseProvider
    from engrix_router.core.types import Credentials, ProviderDef, TransportSpec

    class Wirey(BaseProvider):
        def encode_body(self, payload, *, stream):
            return b"ENC-BODY", {"Encode": "1"}

        def sign_request(self, request, creds):
            request.headers["x-bodylength"] = str(len(request.body_bytes or b""))
            return request

    spec = TransportSpec(base_url="https://x.test/v1", accept_encoding="identity")
    provider = Wirey(ProviderDef(id="wirey", category="apikey", transport=spec))
    creds = Credentials(connection_id="c1", provider="wirey", auth_type="none", name="c1", token=None)
    request = provider.build_request({"model": "wirey/m", "messages": []}, creds, stream=True)

    assert request.body_bytes == b"ENC-BODY"
    assert request.query == {"Encode": "1"}
    assert request.url.endswith("/chat/completions?Encode=1")
    assert request.headers["accept-encoding"] == "identity"
    assert all(k == k.lower() for k in request.headers)   # nama header dinormalisasi
    assert request.headers["x-bodylength"] == "8"
    # build_request dipanggil ulang = body+header baru (replay guard tingkat core)
    second = provider.build_request({"model": "wirey/m", "messages": []}, creds, stream=True)
    assert second.url == request.url and second.headers["x-bodylength"] == "8"


def test_default_provider_tidak_ngubah_wire():
    from engrix_router.providers.base import BaseProvider
    from engrix_router.core.types import Credentials, ProviderDef, TransportSpec

    provider = BaseProvider(ProviderDef(id="plain", category="apikey",
                                        transport=TransportSpec(base_url="https://x.test/v1")))
    creds = Credentials(connection_id="c", provider="plain", auth_type="apikey", name="c", token="sk-1")
    req = provider.build_request({"model": "plain/m", "messages": [{"role": "user", "content": "hi"}]},
                                creds, stream=True)
    assert b'"stream": true' in req.body_bytes
    assert req.query == {}
    assert "accept-encoding" not in req.headers       # httpx negosiasi sendiri
    assert req.headers["authorization"] == "Bearer sk-1"
