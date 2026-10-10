"""
Gateway data contracts: provider definitions, credentials, execution.

This module deliberately has no I/O. Everything here is data plus light
validation, so providers, the pipeline, the API and tests share one language
without import chains.

9router counterpart: providers/schema.js (RegistryEntry) + providers/registry/*.js
+ executors/base.js. Deliberate deviations:
  * a provider has no execute() that builds the URL, signs, sends AND retries in
    one breath. QoderExecutor in 9router overrides execute() wholesale
    (open-sse/executors/qoder.js:613) and as a side effect drops the base.js
    retry ladder - that provider silently ran with 0 retries for versions. Here
    build_request() is pure and the retry ladder lives in pipeline/runner.py.
  * dry-run is a first-class possibility in the contract: build_request() must be
    callable without any network, so a trace can be diffed against 9router
    without burning quota.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Protocol

# ── format & auth ────────────────────────────────────────────────────────────
FORMAT_OPENAI = "openai"
FORMAT_OPENAI_RESPONSES = "openai-responses"
FORMAT_ANTHROPIC = "anthropic"

AUTH_BEARER = "bearer"
AUTH_API_KEY_HEADER = "api_key_header"
AUTH_NONE = "none"


@dataclass(frozen=True)
class AuthSpec:
    """
    How this provider turns a credential into auth headers.

    Keep this declarative: 9router hardcodes the whole decision tree
    (getAuthHeaders, open-sse/services/auth.js:148-212), so a new auth style
    means editing shared service code instead of declaring a provider.

    """

    kind: str = AUTH_BEARER
    header: str = "Authorization"
    prefix: str = "Bearer "
    extra_headers: dict[str, str] = field(default_factory=dict)

    def apply(self, token: str | None) -> dict[str, str]:
        if self.kind == AUTH_NONE or not token:
            return dict(self.extra_headers)
        headers = {self.header: f"{self.prefix}{token}" if self.prefix else token}
        headers.update(self.extra_headers)
        return headers


@dataclass(frozen=True)
class RetrySpec:
    attempts: int
    delay_ms: int


# Default = angka yang sama dipakai 9router (config/runtimeConfig.js:78-83).
# 429 sengaja 0 attempt: di level router, rate limit harus naik ke kandidat
# koneksi lain, bukan di-spam ulang ke upstream yang sama.
DEFAULT_RETRY: dict[int, RetrySpec] = {
    429: RetrySpec(0, 0),
    502: RetrySpec(3, 3000),
    503: RetrySpec(3, 2000),
    504: RetrySpec(2, 3000),
}


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str = ""
    context_length: int | None = None
    max_output: int | None = None
    target_format: str | None = None
    supports_vision: bool = False
    supports_tools: bool = True
    is_default: bool = False

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name or self.id,
            "context_length": self.context_length,
            "max_output": self.max_output,
            "supports_vision": self.supports_vision,
            "supports_tools": self.supports_tools,
        }


@dataclass(frozen=True)
class UsageSpec:
    """How to read vendor quota. mapping = dotted paths to numeric fields."""

    url: str
    method: str = "GET"
    auth: AuthSpec = field(default=AuthSpec())
    mapping: dict[str, str] = field(default_factory=dict)  # {"remaining":"quota.remaining"}
    reset_path: str | None = None
    ttl_s: int | None = None


@dataclass(frozen=True)
class TransportSpec:
    base_url: str
    format: str = FORMAT_OPENAI
    chat_path: str = "/chat/completions"
    models_path: str = "/models"
    auth: AuthSpec = field(default=AuthSpec())
    headers: dict[str, str] = field(default_factory=dict)
    models_url: str | None = None           # kalau beda dari base+models_path
    usage: UsageSpec | None = None
    retry: dict[int, RetrySpec] = field(default_factory=lambda: dict(DEFAULT_RETRY))
    connect_timeout_s: float | None = None
    stall_timeout_s: float | None = None
    url_suffix: str = ""
    accept_encoding: str | None = None           # 'identity' = wajib buat provider bersignature
    send_stream_options: bool = True         # False = upstream nolak param yang gak dikenal
    default_model: str | None = None


@dataclass(frozen=True)
class ProviderDef:
    """One built-in provider. Counterpart of 9router's registry/{id}.js."""

    id: str
    category: str                            # apikey | oauth | none
    transport: TransportSpec
    display_name: str = ""
    brand: str = ""                          # merek untuk nesting dashboard (mis. "Qoder")
    aliases: tuple[str, ...] = ()            # prefix model, mis. ("qd",)
    auth_modes: tuple[str, ...] = ("apikey",)
    models: tuple[ModelSpec, ...] = ()
    features: frozenset[str] = frozenset()   # {"usage"}
    probe_tier: str = "models_list"          # models_list | one_token_chat | key_exchange
    hidden: bool = False

    @property
    def prefixes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.id, *self.aliases)))

    def find_model(self, model_id: str) -> ModelSpec | None:
        for model in self.models:
            if model.id == model_id:
                return model
        return None


# ── kredensial & kandidat ────────────────────────────────────────────────────
@dataclass
class Credentials:
    connection_id: str
    provider: str
    auth_type: str
    name: str
    token: str | None                        # api_key ATAU access_token
    refresh_token: str | None = None
    expires_at: str | None = None
    psd: dict[str, Any] = field(default_factory=dict)
    proxy_url: str | None = None
    proxy_strict: bool = False

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        from datetime import datetime, timezone
        raw = self.expires_at.replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment <= datetime.now(timezone.utc)


@dataclass
class Candidate:
    """One connection worth trying, already carrying its health view."""

    credentials: Credentials
    provider_def: ProviderDef
    priority: int
    test_status: str
    last_used_ms: int | None
    consecutive_use_count: int
    rate_limited_until_ms: int | None

    @property
    def label(self) -> str:
        return self.credentials.name or self.credentials.connection_id[:8]


# ── eksekusi ─────────────────────────────────────────────────────────────────
@dataclass
class ExecRequest:
    """Ready-to-send payload. Built by provider.build_request(), sent by the runner."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    body_bytes: bytes | None = None
    query: dict[str, Any] = field(default_factory=dict)
    method: str = "POST"
    connect_timeout_s: float = 20.0
    first_chunk_timeout_s: float = 25.0
    stall_timeout_s: float = 120.0
    total_timeout_s: float = 300.0
    proxy_url: str | None = None
    tier: str | None = None                     # vendor-level (mis. context tier qoder 1M)


@dataclass
class ProbeResult:
    ok: bool
    tier: str
    status: int | None = None
    error: str | None = None
    latency_ms: int | None = None
    model_probed: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tier": self.tier,
            "status": self.status,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "model_probed": self.model_probed,
            "detail": self.detail,
        }


@dataclass
class QuotaReading:
    scope: str
    used: float | None = None
    total: float | None = None
    remaining: float | None = None
    remaining_pct: float | None = None
    unlimited: bool = False
    reset_at_ms: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class UpstreamError(Exception):
    """Any upstream failure. The runner classifies it; the provider raises it."""

    def __init__(self, message: str, *, status: int | None = None,
                 vendor_code: str | None = None, kind: str = "http") -> None:
        super().__init__(message)
        self.status = status
        self.vendor_code = vendor_code
        self.kind = kind  # http | timeout | transport | stalled | aborted


class Provider(Protocol):
    """Provider contract (all I/O methods are async). See docs/ADDING_A_PROVIDER.md."""

    definition: ProviderDef

    def build_request(self, body: dict[str, Any], creds: Credentials, *, stream: bool) -> ExecRequest:
        """Pure, no network - usable by dry-run and by the trace."""
        ...

    async def list_models(self, creds: Credentials) -> list[ModelSpec]: ...

    async def probe(self, creds: Credentials, model_id: str | None = None) -> ProbeResult: ...

    async def open_stream(self, request: ExecRequest) -> AsyncIterator[dict[str, Any]]: ...

    async def complete(self, request: ExecRequest) -> dict[str, Any]: ...

    async def fetch_quota(self, creds: Credentials) -> list[QuotaReading]: ...
