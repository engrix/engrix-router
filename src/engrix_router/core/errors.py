"""
Gateway error taxonomy -> policy. The only place this map exists.

Why this module exists and why it is this complete: the HTTP status we return to
the client is not decoration, it changes engrix-agent's behaviour drastically.
  * 400/401/403 are treated as a fatal connect error: the agent cancels EVERY
    remaining round (engrix-agent/agent/streaming.py:459-464, 487-488).
  * 401/402/403/432 bench our API key for 86,400 s
    (engrix-agent/key_pool.py:67-85 + config.py:411-412).
  * 429 benches for 60 s, and openai-python also retries internally
    (max_retries is never overridden) => amplification.
  * 5xx is not benched; it enters the agent's own retry ladder (30 s round).
Hard rules of this module:
  1. An upstream quota/health problem is NEVER returned as 403/401. Always
     503 (+Retry-After) or 429 (+Retry-After). 9router synthesises 403 for
     billing blocks (CHANGELOG v0.5.55, open-sse/executors/qoder.js:432-447) and
     that is exactly the "key locked + silent fallback" behaviour this gateway
     refuses to reproduce.
  2. 401/403 are reserved for authentication against THIS gateway (bad client key).
  3. 400 is only for requests that are genuinely malformed (unknown model shape),
     so the client stops wasting retries.

Lock policy per class mirrors config/errorConfig.js:59-86 (401/402/403/404 ->
120 s; rate-limit/quota/capacity/overloaded text -> exponential backoff capped at
5 min; no match -> 30 s; plain 4xx -> NOT locked, accountFallback.js:59-61), plus
the vendor classes 9router handles separately (codes 110 / 112 / 10605 / 103).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# ── kelas error ──────────────────────────────────────────────────────────────
CLASS_CLIENT_BAD_REQUEST = "client_bad_request"
CLASS_CLIENT_NO_KEY = "client_no_key"
CLASS_CLIENT_BAD_KEY = "client_bad_key"
CLASS_MODEL_NOT_FOUND = "model_not_found"
CLASS_RATE_LIMIT = "rate_limited"
CLASS_QUOTA_DAILY = "quota_daily"          # vendor code 110: habis hari ini
CLASS_PRICING_BLOCKED = "pricing_blocked"  # vendor code 112 + pricingUrl
CLASS_QUEUE_THROTTLED = "queue_throttled"  # vendor code 10605
CLASS_CREDENTIAL_DEAD = "credential_dead"  # 401/403 dari upstream = token kita mati
CLASS_PROTOCOL_DRIFT = "protocol_drift"    # signature invalid / replay 103
CLASS_UPSTREAM_UNAVAILABLE = "upstream_unavailable"
CLASS_UPSTREAM_TIMEOUT = "upstream_timeout"
CLASS_STREAM_STALLED = "stream_stalled"
CLASS_BUDGET_EXCEEDED = "budget_exceeded"  # keputusan kita sendiri, bukan vendor
CLASS_ALL_LOCKED = "all_locked"
CLASS_ABORTED = "aborted"
CLASS_UNKNOWN = "unknown"

ALL_CLASSES = (
    CLASS_CLIENT_BAD_REQUEST,
    CLASS_CLIENT_NO_KEY,
    CLASS_CLIENT_BAD_KEY,
    CLASS_MODEL_NOT_FOUND,
    CLASS_RATE_LIMIT,
    CLASS_QUOTA_DAILY,
    CLASS_PRICING_BLOCKED,
    CLASS_QUEUE_THROTTLED,
    CLASS_CREDENTIAL_DEAD,
    CLASS_PROTOCOL_DRIFT,
    CLASS_UPSTREAM_UNAVAILABLE,
    CLASS_UPSTREAM_TIMEOUT,
    CLASS_STREAM_STALLED,
    CLASS_BUDGET_EXCEEDED,
    CLASS_ALL_LOCKED,
    CLASS_ABORTED,
    CLASS_UNKNOWN,
)

# type sesuai ERROR_TYPES 9router (config/errorConfig.js:2-15) supaya SDK klien
# baca error yang dia kenal.
_OPENAI_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    402: "billing_error",
    403: "permission_error",
    404: "invalid_request_error",
    406: "invalid_request_error",
    408: "server_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "server_error",
    503: "server_error",
    504: "server_error",
}


@dataclass(frozen=True)
class Policy:
    """What the gateway does and what the client sees, for one error class."""

    client_status: int
    lock: str            # none | auth | short | transient | backoff | quota_window | model | drift
    retry_internally: bool
    mark_connection: bool
    retry_after_s: int | None


_POLICIES: dict[str, Policy] = {
    CLASS_CLIENT_BAD_REQUEST: Policy(400, "none", False, False, None),
    CLASS_CLIENT_NO_KEY: Policy(401, "none", False, False, None),
    CLASS_CLIENT_BAD_KEY: Policy(401, "none", False, False, None),
    CLASS_MODEL_NOT_FOUND: Policy(404, "none", False, True, None),
    CLASS_RATE_LIMIT: Policy(429, "backoff", False, True, 15),
    CLASS_QUOTA_DAILY: Policy(503, "quota_window", False, True, None),
    CLASS_PRICING_BLOCKED: Policy(503, "model", False, False, 3600),
    CLASS_QUEUE_THROTTLED: Policy(429, "short", True, True, 8),
    CLASS_CREDENTIAL_DEAD: Policy(503, "auth", False, True, 120),
    CLASS_PROTOCOL_DRIFT: Policy(502, "drift", False, True, None),
    CLASS_UPSTREAM_UNAVAILABLE: Policy(503, "transient", True, True, None),
    CLASS_UPSTREAM_TIMEOUT: Policy(504, "transient", True, True, None),
    CLASS_STREAM_STALLED: Policy(504, "transient", False, True, None),
    CLASS_BUDGET_EXCEEDED: Policy(429, "none", False, False, 3600),
    CLASS_ALL_LOCKED: Policy(503, "none", False, False, None),
    CLASS_ABORTED: Policy(499, "none", False, False, None),
    CLASS_UNKNOWN: Policy(502, "transient", False, True, None),
}


@dataclass(frozen=True)
class Classified:
    error_class: str
    policy: Policy
    message: str
    vendor_code: str | None = None
    retry_after_s: int | None = None
    reset_at: datetime | None = None

    @property
    def client_status(self) -> int:
        return self.policy.client_status

    def retry_after_effective(self, default_s: int) -> int | None:
        if self.retry_after_s is not None:
            return self.retry_after_s
        return self.policy.retry_after_s if self.policy.retry_after_s is not None else default_s

    def error_body(self, default_s: int = 15) -> dict[str, Any]:
        status = self.client_status
        return {
            "error": {
                "message": self.message[:900],
                "type": _OPENAI_TYPE.get(status, "server_error"),
                "code": self.error_class,
                "status": status,
                "vendor_code": self.vendor_code,
                "retry_after": self.retry_after_effective(default_s),
                "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            }
        }


# ── deteksi vendor code (string mentah dari upstream) ────────────────────────
_CODE_RE = re.compile(r'"code"\s*:\s*"?(\d+)"?')
_RETRY_AFTER_RE = re.compile(r'"retryAfterSeconds"\s*:\s*(\d+)')
_PRICING_URL_RE = re.compile(r"pricingurl", re.I)
_RATE_TEXT_RE = re.compile(
    r"rate limit|too many requests|quota exceeded|capacity|overloaded|throttl", re.I
)
_SIG_RE = re.compile(r"signature invalid|signature mismatch|invalid signature", re.I)
_REPLAY_RE = re.compile(r"duplicate request", re.I)
_BILLING_TEXT_RE = re.compile(r"billing|daily usage limit|usage limit for chat", re.I)
_EXPIRED_TOKEN_RE = re.compile(r"login expired|token (?:expired|invalid)|invalid_grant", re.I)


def _next_utc_midnight(now: datetime | None = None) -> datetime:
    base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return (base + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


_SPECIAL_CODES = {"103", "110", "112", "10605"}


def classify_vendor_code(raw: str | None) -> str | None:
    """Innermost vendor code wins.

    Qoder wraps errors in layers: `{"code":"403","message":"{\"code\":\"10605\"...}"}` --
    a plain `search()` catches the outer status echo first and the vendor code with
    its own policy is lost. So: if any code found in the body has a special policy
    here, it wins; otherwise the first match.
    """
    if not raw:
        return None
    # Vendor ngirim JSON dalam JSON: kutipan di-escape (`\"code\"`), backslash
    # bikin pola `"code":` gak pernah match. Buang backslash dulu -- ini cuma
    # buat deteksi kode, isi pesan tetap utuh.
    matches = _CODE_RE.findall(raw.replace("\\", ""))
    if not matches:
        return None
    for code in matches:
        if code in _SPECIAL_CODES:
            return code
    return matches[0]


def classify(
    *,
    status: int | None = None,
    text: str | None = None,
    vendor_code: str | None = None,
    exception_name: str | None = None,
    now: datetime | None = None,
) -> Classified:
    """
    Classify one upstream failure into a class + policy.

    The check order deliberately mirrors errorConfig.js (text first, then
    status), because a vendor like Qoder sends 403 for very different things
    (daily billing vs dead token vs replay) and only the text/code separates
    them.

    """
    body = text or ""
    code = vendor_code or classify_vendor_code(body)
    lowered = body.lower()

    def make(error_class: str, message: str, *, retry_after_s: int | None = None,
             reset_at: datetime | None = None) -> Classified:
        return Classified(
            error_class=error_class,
            policy=_POLICIES[error_class],
            message=message,
            vendor_code=code,
            retry_after_s=retry_after_s,
            reset_at=reset_at,
        )

    # 1) protocol drift -- bukan masalah trafik, ini upstream berubah.
    if _SIG_RE.search(lowered) or (code == "103") or _REPLAY_RE.search(lowered):
        return make(CLASS_PROTOCOL_DRIFT, body or "signature/replay rejection dari upstream")
    # 2) kuota harian (kode 110) = jendela kalender, bukan cooldown biasa.
    if code == "110" or (_BILLING_TEXT_RE.search(lowered) and "daily" in lowered):
        reset = _next_utc_midnight(now)
        wait = max(60, int((reset - (now or datetime.now(timezone.utc))).total_seconds()))
        return make(
            CLASS_QUOTA_DAILY,
            body or "daily usage limit tercapai",
            retry_after_s=wait,
            reset_at=reset,
        )
    # 3) model dibayar/di-throttle per model (kode 112 + pricingUrl).
    if code == "112" or _PRICING_URL_RE.search(lowered):
        return make(CLASS_PRICING_BLOCKED, body or "this model requires a paid plan", retry_after_s=3600)
    # 4) antrean (kode 10605).
    if code == "10605":
        wait = _RETRY_AFTER_RE.search(body.replace("\\", ""))
        return make(CLASS_QUEUE_THROTTLED, body or "upstream queue throttle",
                    retry_after_s=max(8, int(wait.group(1))) if wait else 8)
    # 5) rate limit generik.
    if status == 429 or _RATE_TEXT_RE.search(lowered):
        return make(CLASS_RATE_LIMIT, body or "rate limited")
    # 6) kredensial mati (401/403 tanpa tanda billing) -> jangan 403 ke klien.
    if status in (401, 403) or _EXPIRED_TOKEN_RE.search(lowered):
        return make(CLASS_CREDENTIAL_DEAD, body or "upstream rejected the credentials", retry_after_s=120)
    # 7) model gak dikenal.
    if status in (404, 406):
        return make(CLASS_MODEL_NOT_FOUND, body or "model unknown to the upstream")
    # 8) salah request dari sisi kita.
    if status == 400:
        return make(CLASS_CLIENT_BAD_REQUEST, body or "the upstream refused the request shape")
    # 9) timeout/transport.
    if exception_name and ("timeout" in exception_name.lower() or "timed out" in lowered):
        return make(CLASS_UPSTREAM_TIMEOUT, body or f"timeout: {exception_name}")
    if exception_name in {"ConnectError", "RemoteProtocolError", "ReadError", "PoolTimeout"}:
        return make(CLASS_UPSTREAM_UNAVAILABLE, body or f"transport error: {exception_name}")
    # 10) 5xx.
    if status and 500 <= status <= 599:
        return make(CLASS_UPSTREAM_UNAVAILABLE, body or f"upstream {status}")
    return make(CLASS_UNKNOWN, body or (f"unclassified error (status={status})"))


def policy_of(error_class: str) -> Policy:
    return _POLICIES[error_class]


def for_class(error_class: str, message: str, *, retry_after_s: int | None = None,
              vendor_code: str | None = None) -> Classified:
    """
    Build a Classified straight from a class we already decided ourselves.

    Important: do not go through classify(status=429, ...) for internal
    decisions such as budget_exceeded - classify will land on rate_limited
    (429 is literally its rule) and the identity "this is OUR budget call,
    not a vendor rate limit" disappears from the log and from the client's
    error body.

    """
    policy = _POLICIES[error_class]
    return Classified(error_class=error_class, policy=policy, message=message[:900],
                      vendor_code=vendor_code, retry_after_s=retry_after_s)


def client_error_body(error_class: str, message: str, *, retry_after_s: int | None = None) -> dict[str, Any]:
    """Build an error body for a class we decided ourselves (not upstream)."""
    policy = _POLICIES[error_class]
    return {
        "error": {
            "message": message[:900],
            "type": _OPENAI_TYPE.get(policy.client_status, "server_error"),
            "code": error_class,
            "status": policy.client_status,
            "vendor_code": None,
            "retry_after": retry_after_s if retry_after_s is not None else policy.retry_after_s,
            "reset_at": None,
        }
    }
