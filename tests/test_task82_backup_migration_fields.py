"""Kunci dua bug migrasi 9router -> gateway ini.

Bug 1: `expiresAt` tidak ada di _NINE_KEYS, jadi kunci lewat tanpa diterjemahkan
       dan item.get("expires_at") selalu None. Akibatnya pemeriksaan
       Credentials.is_expired tidak pernah jalan (is_expired = False kalau kosong).
Bug 2: 9router menaruh `proxyPoolId` DI DALAM providerSpecificData; peta hanya
       mengenal bentuk root yang tidak pernah ada di backup nyata. proxy_pool_id
       karena itu selalu None, dan semua akun keluar dari IP router sendiri.

Test ini memakai _normalize() asli, bukan salinan.
"""
from __future__ import annotations

import json

import pytest

from engrix_router.api import admin_backup


# ── 1. expiresAt wajib diterjemahkan ─────────────────────────────────────────

def test_normalize_translates_expires_at():
    out = admin_backup._normalize({
        "accessToken": "a",
        "refreshToken": "b",
        "expiresAt": "2027-10-09T20:16:58.059Z",
    })
    assert out["expires_at"] == "2027-10-09T20:16:58.059Z"


def test_normalize_keeps_iso_string_untouched():
    stamp = "2027-08-24T10:46:17.901Z"
    out = admin_backup._normalize({"expiresAt": stamp})
    assert out["expires_at"] == stamp


def test_normalize_does_not_break_canonical_key():
    out = admin_backup._normalize({"expires_at": "2027-01-01T00:00:00Z"})
    assert out["expires_at"] == "2027-01-01T00:00:00Z"


def _creds(expires_at):
    from engrix_router.core.types import Credentials

    return Credentials(
        connection_id="c1", provider="codebuddy", auth_type="oauth",
        name="acc", token="x", expires_at=expires_at)


def test_is_expired_actually_sees_the_value():
    """Nilai yang dulu hilang sekarang sampai ke pemeriksaan kedaluwarsa."""
    out = admin_backup._normalize({"expiresAt": "2020-01-01T00:00:00Z"})
    assert _creds(out.get("expires_at")).is_expired is True


def test_empty_expiry_never_reports_expired():
    """Regresi arah sebaliknya: tanpa expires_at, jangan mengaku kedaluwarsa."""
    assert _creds(None).is_expired is False


# ── 2. proxyPoolId harus digali dari providerSpecificData ────────────────────

def test_normalize_lifts_nested_proxy_pool_id():
    out = admin_backup._normalize({
        "providerSpecificData": {"proxyPoolId": "44844c97-8c6e-41cc-b163-3c899d053fa4"},
    })
    assert out["proxy_pool_id"] == "44844c97-8c6e-41cc-b163-3c899d053fa4"


def test_normalize_prefers_top_level_when_both_present():
    out = admin_backup._normalize({
        "proxyPoolId": "root-id",
        "providerSpecificData": {"proxyPoolId": "nested-id"},
    })
    assert out["proxy_pool_id"] == "root-id"


def test_normalize_ignores_blank_nested_value():
    out = admin_backup._normalize({"providerSpecificData": {"proxyPoolId": "   "}})
    assert "proxy_pool_id" not in out


def test_normalize_survives_none_provider_specific():
    out = admin_backup._normalize({"providerSpecificData": None})
    assert out.get("proxy_pool_id") is None


# ── 3. Kredensial proxy tidak boleh tersimpan di URL ─────────────────────────

def test_split_proxy_url_strips_userinfo():
    host, userinfo = admin_backup._split_proxy_url(
        "http://proxyuser:proxypass@172.17.0.1:7897")
    assert host == "http://172.17.0.1:7897"
    assert userinfo == "proxyuser:proxypass"


def test_split_proxy_url_without_userinfo():
    host, userinfo = admin_backup._split_proxy_url("http://172.17.0.1:7897")
    assert host == "http://172.17.0.1:7897"
    assert userinfo is None


def test_proxy_url_with_userinfo_is_rejected_by_pool_validator():
    """Alasan _split_proxy_url ada: transport.proxy menolak userinfo."""
    from engrix_router.transport import proxy

    with pytest.raises(ValueError):
        proxy._validate_shape("http://proxyuser:proxypass@172.17.0.1:7897")


def test_hint_for_builds_env_name():
    assert admin_backup._hint_for("Aws Proxy") == "EROUTER_PROXY_AWS_PROXY"


# ── 4. Kontrak end-to-end: backup 9router -> baris gateway ───────────────────

def test_backup_document_shape_is_recognised():
    """Bentuk dokumen yang benar-benar dipakai 9router."""
    payload = {
        "settings": {},
        "providerConnections": [{
            "id": "4358b774-f6fc-44bf-9d84-718a41ec58cd",
            "provider": "codebuddy-intl",
            "authType": "oauth",
            "name": "Account 354",
            "priority": 354,
            "isActive": True,
            "accessToken": "token-here",
            "refreshToken": "refresh-here",
            "expiresAt": "2027-10-09T20:16:58.059Z",
            "providerSpecificData": {"proxyPoolId": "pool-abc"},
        }],
        "proxyPools": [{
            "id": "pool-abc",
            "name": "Aws Proxy",
            "proxyUrl": "http://proxyuser:proxypass@172.17.0.1:7897",
            "type": "http",
            "strictProxy": True,
            "isActive": True,
        }],
    }
    item = admin_backup._normalize(payload["providerConnections"][0])
    assert item["expires_at"] == "2027-10-09T20:16:58.059Z"
    assert item["proxy_pool_id"] == "pool-abc"
    assert item["access_token"] == "token-here"
    assert item["refresh_token"] == "refresh-here"
    assert item["is_active"] is True


def test_proxy_pool_import_creates_row_and_maps_id(tmp_path, monkeypatch):
    """_import_proxy_pools membuat baris pool dan mengembalikan peta id lama->baru."""
    from engrix_router.storage import sqlite as storage

    db = tmp_path / "router.sqlite3"
    monkeypatch.setenv("EROUTER_DB_PATH", str(db))
    storage.reset_for_tests(str(db)) if hasattr(storage, "reset_for_tests") else None

    errors: list[dict] = []
    payload = {"proxyPools": [{
        "id": "old-pool-id",
        "name": "Aws Proxy Test",
        "proxyUrl": "http://proxyuser:proxypass@172.17.0.1:7897",
        "type": "http",
        "strictProxy": True,
    }]}
    mapping = admin_backup._import_proxy_pools(payload, errors)

    assert errors == []
    assert mapping.get("old-pool-id")

    from engrix_router.transport import proxy
    pool = proxy.get(mapping["old-pool-id"])
    assert pool is not None
    assert "@" not in pool["proxy_url"], "userinfo tidak boleh tersimpan di URL pool"
    assert pool["strict_proxy"] == 0, "strict dimatikan agar impor tidak mematikan request"
    assert pool["credential_hint"] == "EROUTER_PROXY_AWS_PROXY_TEST"
