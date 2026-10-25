"""Admin API: connection backup -- JSON out, JSON in.

Why this exists: the fleet is hundreds of OAuth accounts whose tokens outlive any
single install by a year, and losing the data directory means re-linking every
account by hand. 9router ships a backup/restore screen; this gateway had none
until the fleet outgrew "copy the sqlite file".

The export CONTAINS SECRETS. That is the point of a backup, and it is exactly
why every route here depends on require_admin (fail-closed 503 without a token),
why the payload is returned as a document and never written to a log line, and
why the import validates each row before writing (one bad row must not abort the
restore of 369 good ones).

Import accepts this gateway's own field names AND 9router's backup keys
(accessToken, refreshToken, providerSpecificData, isActive, authType, proxyPoolId,
nodeId), so an existing 9router backup loads without hand editing.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from engrix_router.accounts import connections
from engrix_router.api import deps
from engrix_router.providers import registry
from engrix_router.transport import proxy

router = APIRouter(prefix="/api", tags=["backup"])

BACKUP_VERSION = 1

# kunci backup 9router -> kunci kanonik. Kunci di luar peta ini diteruskan apa
# adanya: name, email, priority dan api_key sudah sama penulisannya di kedua sisi.
#
# `expiresAt` WAJIB ada di sini. Sebelumnya baris ini tidak ada karena dianggap
# "sudah sama penulisannya di kedua sisi" -- itu salah: 9router menulis camelCase
# `expiresAt`, gateway membaca `expires_at`. Akibatnya kunci lewat tanpa
# diterjemahkan, item.get("expires_at") selalu None, dan seluruh 369 akun masuk
# dengan expires_at kosong. Dampaknya senyap tapi nyata: Credentials.is_expired
# mengembalikan False kalau expires_at kosong, jadi pemeriksaan kedaluwarsa token
# tidak pernah jalan sama sekali.
_NINE_KEYS = {
    "accessToken": "access_token",
    "refreshToken": "refresh_token",
    "expiresAt": "expires_at",
    "providerSpecificData": "provider_specific",
    "isActive": "is_active",
    "authType": "auth_type",
    "proxyPoolId": "proxy_pool_id",
    "nodeId": "node_id",
}


def _normalize(item: dict[str, Any]) -> dict[str, Any]:
    """Translate 9router backup keys into this gateway's canonical keys.

    9router keeps `proxyPoolId` INSIDE providerSpecificData, not at the record
    root. The map above only covers the root shape, and that root shape never
    occurs in a real backup: of 369 accounts, 303 use the nested form and 0 use
    the root form. Without this lift, proxy_pool_id is always None and every
    account leaves from the router's own IP -- a parameter lost during
    migration, not merely an empty column.
    """
    out = {_NINE_KEYS.get(key, key): value for key, value in item.items()}
    psd = out.get("provider_specific")
    if isinstance(psd, dict) and not out.get("proxy_pool_id"):
        nested = psd.get("proxyPoolId")
        if isinstance(nested, str) and nested.strip():
            out["proxy_pool_id"] = nested.strip()
    return out


def _split_proxy_url(raw: str) -> tuple[str, str | None]:
    """Split userinfo off a proxy URL.

    Repo policy (transport/proxy.py) forbids storing userinfo in the proxy_url
    column: the credentials once leaked verbatim into a work report back when
    9router was still in use. Credentials are therefore dropped from the URL and
    only the host is stored; the owner supplies them through the env var that
    credential_hint points at.
    """
    text = (raw or "").strip()
    if "@" not in text:
        return text, None
    scheme, _, rest = text.partition("://")
    userinfo, _, host = rest.rpartition("@")
    return (f"{scheme}://{host}" if scheme else host), userinfo


def _hint_for(name: str) -> str:
    """Env var name holding the pool credentials, e.g. 'Aws Proxy' -> EROUTER_PROXY_AWS_PROXY."""
    slug = "".join(ch if ch.isalnum() else "_" for ch in (name or "pool")).strip("_").upper()
    return f"EROUTER_PROXY_{slug or 'POOL'}"


def _import_proxy_pools(payload: dict[str, Any],
                        errors: list[dict[str, Any]]) -> dict[str, str]:
    """Create proxy_pools rows from the backup document; return old-id -> new-id map.

    Without this step the proxy_pool_id produced by _normalize() is rejected by
    the foreign key (connections.proxy_pool_id REFERENCES proxy_pools(id), and
    PRAGMA foreign_keys = ON), so the proxy parameter stays unrecoverable.

    strict_proxy is deliberately imported as 0 even though 9router stores 1. The
    reason is concrete: strict=1 makes resolve() RAISE when the credential env is
    not filled in, which kills every request from an account using that pool.
    With strict=0 the router falls back to a direct connection while logging a
    warning, so an import can never make the service worse than it already was.
    """
    raw = payload.get("proxyPools")
    mapping: dict[str, str] = {}
    if not isinstance(raw, list):
        return mapping

    existing = {row["name"]: row for row in proxy.list_pools()}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        old_id = str(entry.get("id") or "").strip()
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        if name in existing:
            if old_id:
                mapping[old_id] = existing[name]["id"]
            continue
        url, _userinfo = _split_proxy_url(str(entry.get("proxyUrl") or ""))
        type_ = "socks5" if str(entry.get("type") or "").lower().startswith("socks") else "http"
        try:
            created = proxy.create(
                name=name,
                proxy_url=url,
                no_proxy=entry.get("noProxy") or None,
                type_=type_,
                strict_proxy=False,
                credential_hint=_hint_for(name),
            )
        except ValueError as exc:
            errors.append({"scope": "proxyPool", "name": name, "error": str(exc)})
            continue
        existing[name] = created
        if old_id:
            mapping[old_id] = created["id"]
    return mapping


@router.get("/backup", dependencies=[Depends(deps.require_admin)])
async def export_backup() -> dict[str, Any]:
    """Dump every connection, credentials included, as one JSON document."""
    return {
        "version": BACKUP_VERSION,
        "app": "engrix-router",
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "connections": connections.export_rows(),
    }


@router.post("/backup/import", dependencies=[Depends(deps.require_admin)])
async def import_backup(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore connections from a backup document.

    mode=merge (default): an existing account is matched by id first, then by
    (provider, name); matches are updated (tokens included), the rest created.
    mode=replace: every existing connection is deleted first. Replace also
    demands {"confirm_replace": true} -- one typo there is the whole fleet.
    """
    mode = str(payload.get("mode") or "merge")
    if mode not in {"merge", "replace"}:
        raise HTTPException(400, "mode must be merge or replace")
    raw = payload.get("connections")
    if not isinstance(raw, list):
        raise HTTPException(400, "requires {connections: [...]}")
    if mode == "replace" and not payload.get("confirm_replace"):
        raise HTTPException(400, "replace mode requires confirm_replace=true "
                                 "(it deletes every existing connection first)")

    items = [_normalize(entry) for entry in raw if isinstance(entry, dict)]
    existing = {row["id"]: row
                for row in connections.list_for_provider(None, include_health=False)}
    by_name: dict[tuple[str, str], str] = {}
    for row in existing.values():
        if row.get("provider") and row.get("name"):
            by_name[(row["provider"], row["name"])] = row["id"]

    created = updated = skipped = 0
    errors: list[dict[str, Any]] = []
    if mode == "replace":
        for row in existing.values():
            connections.delete(row["id"])
        existing, by_name = {}, {}

    # Pool proxy harus ada SEBELUM connections ditulis: kolomnya foreign key ke
    # proxy_pools, dan PRAGMA foreign_keys = ON menolak id yang tidak punya baris.
    pool_map = _import_proxy_pools(payload, errors)

    for index, item in enumerate(items):
        provider = str(item.get("provider") or "").strip()
        if not provider:
            skipped += 1
            errors.append({"index": index, "error": "missing provider"})
            continue
        definition = registry.get_definition(provider)
        if definition is None:
            skipped += 1
            errors.append({"index": index, "error": f"provider '{provider}' is unknown"})
            continue
        # Nama kanonik: backup 9router menulis 'codebuddy-intl' (alias), dan
        # kalau baris connections dibuat dengan nama alias itu, candidates()
        # (WHERE provider = definition.id) TIDAK PERNAH melihatnya -- akun
        # masuk tapi gak pernah dipakai. Selalu simpan id kanonik.
        provider = definition.id
        name = item.get("name")
        psd = item.get("provider_specific") if isinstance(item.get("provider_specific"), dict) else None
        target_id = (item.get("id") if item.get("id") in existing
                     else by_name.get((provider, name)))
        try:
            if target_id:
                # None = "tidak diberikan" dan TIDAK dikirim: connections.update()
                # menulis setiap kunci yang ada di patch, dan priority penting
                # NOT NULL -- backup tanpa priority harus memperlakukan kolom itu
                # sebagai "biarkan", bukan "kosongkan".
                patch = {key: value for key, value in {
                    "name": name,
                    "email": item.get("email"),
                    "priority": item.get("priority"),
                    "api_key": item.get("api_key"),
                    "access_token": item.get("access_token"),
                    "refresh_token": item.get("refresh_token"),
                    "expires_at": item.get("expires_at"),
                    "proxy_pool_id": pool_map.get(str(item.get("proxy_pool_id")))
                    or item.get("proxy_pool_id"),
                }.items() if value is not None}
                patch["is_active"] = bool(item.get("is_active", True))
                if psd is not None:
                    patch["provider_specific"] = psd
                if connections.update(target_id, patch) is not None:
                    updated += 1
                else:
                    skipped += 1
            else:
                connections.create(
                    provider=provider,
                    auth_type=str(item.get("auth_type")
                                  or ("oauth" if item.get("access_token") else "apikey")),
                    name=name,
                    email=item.get("email"),
                    api_key=item.get("api_key"),
                    access_token=item.get("access_token"),
                    refresh_token=item.get("refresh_token"),
                    expires_at=item.get("expires_at"),
                    priority=item.get("priority"),
                    provider_specific=psd,
                    proxy_pool_id=pool_map.get(str(item.get("proxy_pool_id")))
                    or item.get("proxy_pool_id"),
                    is_active=bool(item.get("is_active", True)),
                )
                created += 1
        except ValueError as exc:
            skipped += 1
            errors.append({"index": index, "name": name, "error": str(exc)})
    return {"mode": mode, "total": len(items), "created": created, "updated": updated,
            "skipped": skipped, "errors": errors[:20]}
