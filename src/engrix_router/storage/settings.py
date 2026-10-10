"""
Runtime settings an operator can change through the API without a restart.

This is the only place defaults are defined (DEFAULTS) and the only place types
are validated. Values are read from the `settings` table (key -> JSON) with an
in-memory cache that is invalidated explicitly; writes go through set_setting()
so invalidation cannot be forgotten.

Rules: a new key is one entry in DEFAULTS. Do not call os.getenv here (that is
core/config.py's job), and never store secrets here - 9router keeps its dashboard
bcrypt password in the settings blob, which is exactly what we do not repeat.
"""
from __future__ import annotations

import json
import threading
from typing import Any

from engrix_router.storage.sqlite import date_key_utc, execute, now_ms, query

SETTINGS_VERSION = 1

# Nama kunci memakai prefix domain supaya grep dan autocomplete jelas,
# dan supaya gak ada tabrakan antar-subsystem.
DEFAULTS: dict[str, tuple[Any, type, str]] = {
    # ── routing strategy (padanan settings.fallbackStrategy 9router) ──
    "routing.strategy": ("fill-first", str, "fill-first | round-robin | least-recent"),
    "routing.sticky_round_robin_limit": (3, int, "how many consecutive requests stick to the same account"),
    "routing.per_provider": ({}, dict,
                             "{'<provider id>': {'strategy': 'round-robin', 'sticky_round_robin_limit': 1}}"),
    "routing.max_connections_per_request": (100, int, "how many candidates one request may iterate"),
    # ── kesehatan koneksi (padanan config/errorConfig.js) ──
    "health.cooldown_auth_ms": (120_000, int, "401/402/403/404 -> how long to lock"),
    "health.cooldown_transient_ms": (30_000, int, "unclassified error: how long to lock"),
    "health.cooldown_short_ms": (5_000, int, "request-not-allowed style"),
    "health.anti_abuse_cooldown_ms": (120_000, int,
                                      "anti-abuse rejection (ZCode 3012): how long to lock that model only, account stays usable"),
    "health.backoff_base_ms": (2_000, int, "rate limit: 2^level * base"),
    "health.backoff_max_ms": (300_000, int, "cap backoff rate limit"),
    "health.backoff_max_level": (15, int, "cap level backoff"),
    "health.lock_plain_4xx": (False, bool, "400/406 may lock an account? default NO (anti self-DoS guard)"),
    "health.protocol_drift_threshold": (2, int, "how many sig_invalid/replay hits before the gateway freezes every upstream"),
    "health.self_heal_unavailable": (True, bool, "unavailable may return to cooling on its own once every lock expired"),
    # Status, bukan konfigurasi -- tapi kita persist di tabel settings supaya
    # tripped-nya gak ilang kalau proses restart (9router: cache health di RAM,
    # restart = lupa; lihat antigravityQuota.js:11-31).
    "health.drift_until_ms": (0, int, "epoch-ms until which the gateway refuses every upstream call"),
    "health.drift_reason": ("", str, "reason for the drift trip, shown in the dashboard"),
    # ── error mapping ke klien (kontrak engrix-agent) ──
    "client.retry_after_default_s": (15, int, "Retry-After for 429/503 when the vendor gives no hint"),
    # ── cache ──
    "catalog.ttl_s": (3600, int, "cache /models per connection"),
    "quota.ttl_s": (30, int, "cache a quota snapshot this long before calling the vendor again"),
    # ── rate limiting jendela pendek (detik, in-process) ──
    # Default 0 = mati: engrix punya retry ladder sendiri dan limiter yang nyala
    # tanpa diminta bikin dia dapat 429 yang gak dia paham asalnya.
    "ratelimit.requests_per_minute_per_key": (0, int,
                                              "sliding 60s window per gateway client key, 0 = disabled"),
    "ratelimit.requests_per_minute_per_provider": (0, int,
                                                   "sliding 60s window per provider, 0 = disabled"),
    # ── budget harian (UTC) ──
    "budget.global_tokens_per_day": (0, int, "0 = unlimited"),
    "budget.global_requests_per_day": (0, int, "0 = unlimited"),
    "budget.per_provider_tokens_per_day": ({}, dict, "{'<provider id>': 4000000}"),
    "budget.per_key_tokens_per_day": ({}, dict, "{'<api_key_id>': 1000000}"),
    "budget.reserve_headroom_tokens": (8192, int, "completion headroom held while a call is reserved"),
    # ── harga ──
    "pricing.overrides": ({}, dict, "{'openai': {'gpt-4o-mini': {'input':0.15,'output':0.6,'cached':0.075}}}"),
    # ── observability ──
    "observability.enabled": (True, bool, "record request_stages (the per-request trace)"),
    # ── layanan latar belakang ──
    # 0 = mati. Sinkronisasi kuota itu murah tapi tetap panggilan ke vendor, jadi
    # nyala cuma kalau operator minta; bisa dipicu manual lewat POST /api/quota.
    "services.quota_sync_interval_s": (0, int, "how often the quota job asks every vendor; 0 = disabled"),
    "observability.max_records": (5000, int, "row cap for the requests table"),
    "observability.max_stage_bytes": (65536, int, "truncate payloads larger than this"),
    "observability.retention_days": (90, int, "delete requests older than this"),
    # ── provider/node guard ──
    "nodes.allow_private_urls": (False, bool, "SSRF guard: False = reject 127/10/172.16/192.168/169.254 targets"),
    "nodes.validate_before_create": (True, bool, "call the upstream /models endpoint before saving a node"),
    # ── knob per vendor ──
    # Sengaja KOSONG di core publik. Vendor khusus (mis. adapter subscription
    # privat) daftar sendiri lewat register_defaults() pas modulnya di-import --
    # itu yang bikin "nambah vendor" gak pernah nyentuh file ini.
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


def register_defaults(mapping: dict[str, tuple[Any, type, str]], *, owner: str) -> None:
    """
    Let a provider package declare its own knobs instead of editing this module.

    Keys must be prefixed with the provider id (``<provider id>.encode_body``), because the
    prefix is what keeps two adapters from fighting over one key and what makes
    ``grep`` find the owner of a setting. Registering a key twice with a different
    default is a hard error: silently overwriting a default would change behaviour
    depending on import order.
    """
    with _lock:
        for key, entry in mapping.items():
            if not key.startswith(f"{owner}."):
                raise ValueError(f"settings key '{key}' must start with the provider id '{owner}.'")
            existing = DEFAULTS.get(key)
            if existing is not None and existing != entry:
                raise ValueError(f"settings key '{key}' is already registered with a different default")
            DEFAULTS[key] = entry
        _cache = None


def _load() -> dict[str, Any]:
    merged = {key: default for key, (default, _typ, _doc) in DEFAULTS.items()}
    for row in query("SELECT key, value_json FROM settings"):
        if row["key"] not in DEFAULTS:
            continue  # kunci buangan dari versi lama: diabaikan, jangan crash
        try:
            merged[row["key"]] = json.loads(row["value_json"])
        except json.JSONDecodeError:
            continue
    return merged


def all_settings(*, refresh: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None or refresh:
            _cache = _load()
        return dict(_cache)


def get(key: str, default: Any = None) -> Any:
    if key not in DEFAULTS:
        if default is None:
            raise KeyError(f"settings key is not registered: {key} (see DEFAULTS / register_defaults)")
        return default
    value = all_settings().get(key)
    if value is None:
        return DEFAULTS[key][0]
    return value


def get_int(key: str) -> int:
    return int(get(key))


def get_bool(key: str) -> bool:
    return bool(get(key))


def get_dict(key: str) -> dict[str, Any]:
    value = get(key)
    return value if isinstance(value, dict) else {}


def set_setting(key: str, value: Any, *, actor: str = "api") -> dict[str, Any]:
    """Write one key with type validation, and invalidate the cache."""
    global _cache
    if key not in DEFAULTS:
        raise KeyError(f"settings key is not registered: {key}")
    _default, typ, _doc = DEFAULTS[key]
    if typ is bool and not isinstance(value, bool):
        value = str(value).strip().lower() in {"1", "true", "yes", "on"}
    elif typ is int and not isinstance(value, bool):
        value = int(value)
    elif typ is dict and not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    execute(
        "INSERT INTO settings(key, value_json, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
        (key, json.dumps(value), now_ms()),
    )
    with _lock:
        _cache = None
    return {"key": key, "value": get(key), "actor": actor}


def strategy_for(provider: str) -> dict[str, Any]:
    """Effective strategy per provider: override beats global (same shape as 9router's settings.providerStrategies, sse/services/auth.js:140-142)."""
    overrides = get_dict("routing.per_provider")
    entry = overrides.get(provider) or {}
    return {
        "strategy": entry.get("strategy") or get("routing.strategy"),
        "sticky_limit": int(entry.get("sticky_round_robin_limit", get_int("routing.sticky_round_robin_limit"))),
    }


def budget_scope_for_today() -> str:
    return date_key_utc()
