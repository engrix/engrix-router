"""URL guard: shape validation + SSRF protection for user-controlled input.

Used when creating an OpenAI-compatible node (base_url typed by hand) and when
setting proxy_url. 9router has a guard too (`assertPublicUrl`, called from
/api/provider-nodes/validate) -- that is the one we copy, because without a guard
a node can be pointed at 169.254.169.254 (AWS metadata) or 127.0.0.1:20128 and
the gateway becomes an internal browser using our own credentials.

The hosts this gateway is deployed on sit inside cloud VPCs, so that metadata
endpoint is a real target, not a theory.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from engrix_router.storage import settings

_ALLOWED_SCHEMES = frozenset({"https", "http"})
_LABEL = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


def normalize_base_url(raw: str, *, strip_paths: tuple[str, ...] = ("/chat/completions", "/models", "/embeddings", "/v1")) -> str:
    value = (raw or "").strip()
    if not value:
        raise ValueError("url is empty")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported scheme: {parsed.scheme}")
    if not parsed.hostname:
        raise ValueError("url without host")
    path = parsed.path.rstrip("/")
    for suffix in strip_paths:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    rebuilt = f"{parsed.scheme}://{parsed.netloc}{path}"
    return rebuilt


def is_public_target(url: str) -> tuple[bool, str]:
    """(allowed, reason). Checks the IP literal plus hostnames that look internal."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").strip(".")
    if not host:
        return False, "host is empty"
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        return False, f"internal host: {host}"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not all(_LABEL.match(label) for label in host.split(".") if label):
            return False, f"invalid hostname: {host}"
        return True, "ok"
    if address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast or address.is_unspecified:
        return False, f"IP is not publicly routable: {address}"
    if address.is_private:
        return False, f"private IP: {address} (set nodes.allow_private_urls if it is genuinely internal)"
    return True, "ok"


def assert_safe_url(url: str, *, kind: str = "base_url") -> str:
    clean = normalize_base_url(url)
    allowed = settings.get_bool("nodes.allow_private_urls")
    ok, reason = is_public_target(clean)
    if not ok and not allowed:
        raise ValueError(f"{kind} rejected ({reason})")
    return clean
