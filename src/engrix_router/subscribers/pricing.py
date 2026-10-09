"""Price per 1M tokens -> USD cost.

Structure copied from open-sse/providers/pricing.js (rate $/1M, five slots
{input, output, cached, reasoning, cache_creation}, resolution
provider-override -> free-namespace -> canonical -> pattern -> None) with a much
smaller table: only the models that genuinely pass through here are needed, and
an unknown value must stay None (cost 0 + flag), never an invented number.

Three behaviours protected by the tests in tests/test_pricing.py:
  * prompt is treated as ALREADY including cached -> nonCached = max(0, prompt -
    cached - cache_creation) (pricing.js:468-470);
  * the free namespace is checked BEFORE canonical, so qoder/qfmodel does not
    inherit a "qwen" price that merely happens to exist;
  * a model with no table entry -> cost 0, not None, which would crash the dashboard.
"""
from __future__ import annotations

import fnmatch
from typing import Any

RATE_KEYS = ("input", "output", "cached", "reasoning", "cache_creation")

# $ per 1M token. Sumber baris ditandai; angka yang belum diverifikasi owner
# dikosongkan (None) supaya tidak jadi biaya palsu.
MODEL_PRICING: dict[str, dict[str, float | None]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cached": 0.075, "reasoning": None, "cache_creation": None},
    "gpt-4o": {"input": 2.50, "output": 10.00, "cached": 1.25, "reasoning": None, "cache_creation": None},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60, "cached": 0.10, "reasoning": None, "cache_creation": None},
    "o3-mini": {"input": 1.10, "output": 4.40, "cached": 0.55, "reasoning": 4.40, "cache_creation": None},
}

PATTERN_PRICING: tuple[tuple[str, dict[str, float | None]], ...] = (
    ("gpt-4o*", MODEL_PRICING["gpt-4o"]),
    ("o3-*", MODEL_PRICING["o3-mini"]),
)

# Provider yang faktanya gratis untuk model tertentu (qfmodel price_factor 0.0,
# diverifikasi dari katalog vendor). Nilainya nol, bukan None: "gratis" dan
# "tidak diketahui" dua hal berbeda.
FREE_NAMESPACES: dict[str, frozenset[str]] = {
    "qoder": frozenset({"qfmodel"}),
    "ollama": frozenset({"*"}),
}

ZERO = {key: 0.0 for key in RATE_KEYS}


def _strip_provider_prefix(model: str) -> str:
    return model.split("/")[-1] if "/" in model else model


def _override_for(provider: str) -> dict[str, Any]:
    from engrix_router.storage import settings

    block = settings.get_dict("pricing.overrides").get(provider)
    return block if isinstance(block, dict) else {}


def resolve(provider: str, model: str) -> dict[str, float | None] | None:
    override = _override_for(provider)
    if isinstance(override, dict):
        entry = override.get(model) or override.get(_strip_provider_prefix(model))
        if isinstance(entry, dict):
            return {key: entry.get(key) for key in RATE_KEYS}
    namespace = FREE_NAMESPACES.get(provider)
    if namespace:
        bare = _strip_provider_prefix(model)
        if "*" in namespace or bare in namespace or model in namespace:
            return dict(ZERO)
    bare = _strip_provider_prefix(model)
    if bare in MODEL_PRICING:
        return dict(MODEL_PRICING[bare])
    if model in MODEL_PRICING:
        return dict(MODEL_PRICING[model])
    for pattern, rates in PATTERN_PRICING:
        if fnmatch.fnmatch(bare, pattern):
            return dict(rates)
    return None


def calculate(usage: dict[str, Any], pricing: dict[str, float | None] | None) -> float:
    """USD cost. usage = the canonical shape (router/usage.canonicalize)."""
    if not pricing:
        return 0.0
    prompt = int(usage.get("prompt") or 0)
    completion = int(usage.get("completion") or 0)
    cached = int(usage.get("cached") or 0)
    cache_creation = int(usage.get("cache_creation") or 0)
    reasoning = int(usage.get("reasoning") or 0)
    non_cached = max(0, prompt - cached - cache_creation)
    input_rate = pricing.get("input")
    output_rate = pricing.get("output")
    cost = 0.0
    if input_rate is not None:
        cost += non_cached * float(input_rate) / 1e6
    if cached and pricing.get("cached") is not None:
        cost += cached * float(pricing["cached"]) / 1e6
    if cache_creation and pricing.get("cache_creation") is not None:
        cost += cache_creation * float(pricing["cache_creation"]) / 1e6
    if completion:
        effective_output = output_rate
        if reasoning and pricing.get("reasoning") is not None:
            billed_plain = max(0, completion - reasoning)
            cost += billed_plain * float(effective_output or 0) / 1e6
            cost += reasoning * float(pricing["reasoning"]) / 1e6
            return round(cost, 8)
        if effective_output is not None:
            cost += completion * float(effective_output) / 1e6
    return round(cost, 8)


def cost_for(provider: str, model: str, usage: dict[str, Any]) -> tuple[float, bool]:
    """(cost, known) -- known is False when there is no price table, so the UI can
    show 'unknown' instead of a '$0.00' that looks like free."""
    pricing = resolve(provider, model)
    if pricing is None:
        return 0.0, False
    return calculate(usage, pricing), True


def table_snapshot() -> dict[str, Any]:
    return {
        "rates_unit": "USD per 1M tokens",
        "model_pricing": MODEL_PRICING,
        "patterns": [pattern for pattern, _ in PATTERN_PRICING],
        "free_namespaces": {key: sorted(value) for key, value in FREE_NAMESPACES.items()},
    }
