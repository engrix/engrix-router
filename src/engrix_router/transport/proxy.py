"""Proxy pool: CRUD, per-connection resolution, and probe.

9router counterpart: the proxyPools table + src/lib/network/connectionProxy.js.
What we do NOT copy over:
  * proxy rotation in 9router only runs for free no-auth providers
    (connectionProxy.js:18-34 used by auth.js:47-57); an authenticated connection
    (qoder included) gets a STATIC pool and a proxy failure never shifts the pool
    or updates testStatus (only the dashboard button writes it). We behave the
    same on the data path (static, deterministic), but its probe may be called
    automatically.
  * proxy userinfo is NOT stored in the DB. The proxy_url column of our table
    must be free of credentials; they come from env (credential_hint ->
    EROUTER_PROXY_*). The reason is concrete: 9router stored proxy credentials
    on the pool row, and they once leaked in full into a plaintext work report
    -- we are not repeating that.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit

from engrix_router.storage.sqlite import execute, now_iso, query, query_one
from engrix_router.core import logs as applog
from engrix_router.core.ids import new_id

PROBE_URL_DEFAULT = "https://openapi.qoder.sh/api/v1/userinfo"


def _validate_shape(proxy_url: str) -> str:
    value = proxy_url.strip()
    if not value:
        raise ValueError("proxy_url is empty")
    parts = urlsplit(value if "//" in value else f"http://{value}")
    if not parts.hostname:
        raise ValueError(f"invalid proxy_url: {proxy_url}")
    if parts.username or parts.password:
        raise ValueError(
            "proxy_url must not contain userinfo; keep credentials in env "
            "and set credential_hint (e.g. EROUTER_PROXY_POOL_A)"
        )
    if parts.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError(f"unsupported proxy scheme: {parts.scheme}")
    return value if "//" in value else f"http://{value}"


def create(*, name: str, proxy_url: str, no_proxy: str | None = None, type_: str = "http",
           strict_proxy: bool = False, credential_hint: str | None = None) -> dict[str, Any]:
    if not name or not name.strip():
        raise ValueError("name is required")
    if type_ not in {"http", "socks5"}:
        raise ValueError(f"unknown proxy type: {type_}")
    pool_id = new_id()
    clean = _validate_shape(proxy_url)
    execute(
        "INSERT INTO proxy_pools(id, name, proxy_url, no_proxy, type, strict_proxy, credential_hint,"
        " is_active, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (pool_id, name.strip(), clean, no_proxy, type_, 1 if strict_proxy else 0, credential_hint,
         1, now_iso(), now_iso()),
    )
    applog.info(applog.NS_PROXY, f"pool created: {name.strip()} -> {clean}")
    return get(pool_id) or {"id": pool_id}


def update(pool_id: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    allowed = {"name", "no_proxy", "type", "strict_proxy", "is_active", "credential_hint"}
    columns = {key: value for key, value in patch.items() if key in allowed}
    if "proxy_url" in patch and patch["proxy_url"]:
        columns["proxy_url"] = _validate_shape(str(patch["proxy_url"]))
    if not columns:
        return get(pool_id)
    columns["updated_at"] = now_iso()
    sets = ", ".join(f"{key} = ?" for key in columns)
    params = [1 if value is True else 0 if value is False else value for value in columns.values()]
    params.append(pool_id)
    execute(f"UPDATE proxy_pools SET {sets} WHERE id = ?", params)  # noqa: S608 -- key dari whitelist
    return get(pool_id)


def delete(pool_id: str) -> bool:
    return execute("DELETE FROM proxy_pools WHERE id = ?", (pool_id,)) > 0


def list_pools(*, include_usage: bool = False) -> list[dict[str, Any]]:
    rows = [dict(row) for row in query("SELECT * FROM proxy_pools ORDER BY created_at")]
    if include_usage:
        for row in rows:
            count = query_one(
                "SELECT COUNT(*) AS n FROM connections WHERE proxy_pool_id = ?", (row["id"],)
            )
            row["bound_connection_count"] = int(count["n"]) if count else 0
    return rows


def get(pool_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM proxy_pools WHERE id = ?", (pool_id,))
    return dict(row) if row else None


def _attach_credentials(pool: dict[str, Any]) -> str:
    hint = (pool.get("credential_hint") or "").strip()
    if not hint:
        return pool["proxy_url"]
    secret = os.getenv(hint, "").strip()
    if not secret:
        raise RuntimeError(f"credential_hint '{hint}' is missing from the environment")
    parts = urlsplit(pool["proxy_url"])
    host = parts.netloc
    scheme = parts.scheme
    port = f":{parts.port}" if parts.port else ""
    return f"{scheme}://{secret}@{host}{port}"


def resolve(proxy_pool_id: str | None, psd: dict[str, Any] | None = None) -> tuple[str | None, bool]:
    """(url, strict). psd may override: connectionProxyEnabled=false = off."""
    psd = psd or {}
    if psd.get("proxy_enabled") is False:
        return None, False
    override = (psd.get("proxy_url") or "").strip()
    if override and not proxy_pool_id:
        return _validate_shape(override), bool(psd.get("proxy_strict", False))
    if not proxy_pool_id:
        return None, False
    pool = get(proxy_pool_id)
    if pool is None or not pool["is_active"]:
        enabled = bool(psd.get("proxy_required", False))
        if enabled:
            raise RuntimeError(f"proxy pool {proxy_pool_id} is inactive/deleted, this connection requires it")
        return None, False
    try:
        url = _attach_credentials(pool)
    except RuntimeError:
        if pool["strict_proxy"]:
            raise
        applog.warn(applog.NS_PROXY, f"pool {pool['name']}: credentials missing, continuing without proxy")
        return None, False
    return url, bool(pool["strict_proxy"])


def record_test_result(pool_id: str, *, ok: bool, error: str | None = None,
                       status: int | None = None, latency_ms: int | None = None) -> dict[str, Any] | None:
    execute(
        "UPDATE proxy_pools SET test_status=?, last_tested_at=?, last_error=?, updated_at=?"
        " WHERE id = ?",
        ("active" if ok else "error", now_iso(),
         None if ok else f"status={status} {error}"[:400] if (status or error) else None,
         now_iso(), pool_id),
    )
    return get(pool_id)


async def test(pool_id: str, *, probe_url: str | None = None) -> dict[str, Any]:
    """Real probe through the proxy. Needs outbound network access; probe_url can be
    pointed at an internal endpoint for offline tests."""
    import httpx

    pool = get(pool_id)
    if pool is None:
        raise ValueError(f"pool {pool_id} does not exist")
    url = _attach_credentials(pool)
    target = probe_url or os.getenv("EROUTER_PROXY_TEST_URL") or PROBE_URL_DEFAULT
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=url, timeout=httpx.Timeout(15.0, connect=8.0)) as client:
            response = await client.get(target)
        latency = int((time.perf_counter() - started) * 1000)
        ok = response.status_code < 400
        record_test_result(pool_id, ok=ok, status=response.status_code,
                           error=None if ok else f"HTTP {response.status_code}", latency_ms=latency)
        return {"ok": ok, "status": response.status_code, "latency_ms": latency,
                "target": target, "pool": pool["name"]}
    except Exception as exc:  # pragma: no cover - jalur jaringan
        latency = int((time.perf_counter() - started) * 1000)
        record_test_result(pool_id, ok=False, error=f"{exc.__class__.__name__}: {exc}", latency_ms=latency)
        return {"ok": False, "status": None, "latency_ms": latency, "target": target,
                "pool": pool["name"], "error": f"{exc.__class__.__name__}: {exc}"[:300]}
