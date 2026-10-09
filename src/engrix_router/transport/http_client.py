"""
httpx AsyncClient factory, cached per (proxy, event loop).

Why the pool is cached here rather than per request: 9router cannot explain why
its path got flagged server-side while a byte-identical payload from fresh
python/node passed (the working theory: the ClientHello/TLS fingerprint of a
long-lived pooled connection). If we opened a connection per
request our fingerprint would keep changing; we need ONE place that owns the
policy so it can be A/B tested later. That is why pooling is its own module and
not scattered through the providers.
"""
from __future__ import annotations

import asyncio
import threading

import httpx

from engrix_router.core import config

_clients: dict[tuple[str, int], httpx.AsyncClient] = {}
# threading.Lock, bukan asyncio.Lock level modul: yang terakhir ngikat diri ke
# loop pertama dan gagal di loop lain (lihat catatan di accounts/catalog.py).
# Konstructor AsyncClient itu sinkron, jadi lock biasa cukup dan tidak bikin
# event loop nunggu.
_lock = threading.Lock()

DEFAULT_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=24, keepalive_expiry=30.0)


def _cache_key(proxy_url: str | None) -> tuple[str, int]:
    """
    (proxy, running event loop id) - not proxy alone.

    An AsyncClient binds its transport to the loop it was created on. A cache
    keyed by proxy only becomes a time bomb as soon as a second loop exists:
    TestClient builds one per `with client:`, a script calling asyncio.run()
    twice makes another, and what breaks is not the request but `aclose()` at
    shutdown ('Event loop is closed' - the real failure of the first version
    of the private adapter's live check script). In a server there is exactly
    one loop, so stale entries are simply dropped and behaviour is unchanged.

    """
    return (proxy_url or "__direct__", id(asyncio.get_running_loop()))


async def get_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    key = _cache_key(proxy_url)
    with _lock:
        for stale in [existing for existing in _clients if existing[1] != key[1]]:
            _clients.pop(stale, None)  # loop mati: tinggalkan, jangan dipakai
        client = _clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                limits=DEFAULT_LIMITS,
                proxy=proxy_url or None,
                timeout=httpx.Timeout(
                    config.UPSTREAM_TOTAL_TIMEOUT_S,
                    connect=config.UPSTREAM_CONNECT_TIMEOUT_S,
                    read=config.UPSTREAM_STALL_TIMEOUT_S,
                ),
                follow_redirects=False,
            )
            _clients[key] = client
        return client


async def aclose_all() -> None:
    with _lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        await client.aclose()


def open_connections() -> dict[str, int]:
    """Introspection for /health - how many pools live and how many sockets are open."""
    out: dict[str, int] = {}
    for (proxy_key, _loop), client in _clients.items():
        pool = getattr(client, "_pool", None)
        connections = getattr(pool, "_connections", None)
        out[proxy_key] = len(connections) if connections is not None else 0
    return out
