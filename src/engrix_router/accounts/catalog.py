"""
Cache of the upstream model list, per connection.

Counterpart of open-sse/services/qoderModels.js:38-39 (1 hour TTL, in-flight
dedupe so two first requests do not double-fetch) plus providers/catalogOverride.js
(mtime reload). We deliberately do NOT copy its 24 hour models.dev sync that
rebuilds the whole model-catalog.json - that is the vision-model bug source
that once forced a 5 minute external rewrite timer. The catalog here is: ask
upstream, cache, use.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from engrix_router.core import logs as applog
from engrix_router.storage import settings
from engrix_router.core.types import Credentials, ModelSpec, Provider

_TTL_FALLBACK_S = 3600
_cache: dict[str, tuple[float, list[ModelSpec]]] = {}
_inflight: dict[str, asyncio.Task] = {}
# Sengaja TIDAK pakai asyncio.Lock() level modul: lock 3.10+ ngikat dirinya ke
# loop pertama yang makenya, jadi di bawah TestClient / multi-loop uvicorn dia
# raise "attached to a different loop" dan exception-nya ketelan di compat.list_models
# (ini yang bikin /v1/models kosong pas verifikasi pertama). `dict.setdefault`
# cukup buat dedupe in-flight: atomic di CPython, dan race terburuknya cuma
# satu fetch ganda -- bukan salah hasil.


def _key(provider: str, connection_id: str) -> str:
    return f"{provider}:{connection_id}"


def cached(provider: str, connection_id: str) -> list[ModelSpec] | None:
    entry = _cache.get(_key(provider, connection_id))
    if not entry:
        return None
    fetched_at, models = entry
    ttl = settings.get_int("catalog.ttl_s") or _TTL_FALLBACK_S
    if time.time() - fetched_at > ttl:
        return None
    return models


def invalidate(provider: str | None = None, connection_id: str | None = None) -> int:
    if provider and connection_id:
        return 1 if _cache.pop(_key(provider, connection_id), None) else 0
    doomed = [key for key in _cache if not provider or key.startswith(f"{provider}:")]
    for key in doomed:
        _cache.pop(key, None)
    return len(doomed)


async def models_for(provider: Provider, creds: Credentials, *, force: bool = False) -> list[ModelSpec]:
    """
    Model list: cache > in-flight join > upstream fetch > built-in fallback.

    The instance is passed in, not looked up here: accounts and providers are
    one band, so the caller resolves the provider. When the upstream refuses
    to list /models at all, we fall back to the provider's built-in
    ModelSpec list instead of returning empty - an empty list makes the
    dashboard look broken when only one capability died.

    """
    definition = provider.definition
    if not force:
        hit = cached(definition.id, creds.connection_id)
        if hit is not None:
            return hit
    key = _key(definition.id, creds.connection_id)
    task = _inflight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(provider.list_models(creds))
        _inflight[key] = task
    try:
        models = await task
    except Exception as exc:
        _inflight.pop(key, None)
        applog.warn(applog.NS_PROVIDER, f"list_models {definition.id} gagal: {exc.__class__.__name__}")
        return list(definition.models)
    _inflight.pop(key, None)
    if not models:
        models = list(definition.models)
    _cache[key] = (time.time(), models)
    return models


def as_public_dicts(models: list[ModelSpec]) -> list[dict[str, Any]]:
    return [model.as_public_dict() for model in models]


def stats() -> dict[str, Any]:
    return {
        "entries": len(_cache),
        "keys": sorted(_cache.keys()),
        "inflight": sorted(_inflight.keys()),
        "ttl_s": settings.get_int("catalog.ttl_s"),
    }
