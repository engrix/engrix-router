"""
Usage canonicalisation: extract, merge, fold cache, estimate.

Counterpart of open-sse/utils/usageTracking.js. What we DROP, and why:
  * BUFFER_TOKENS = 2000 (usageTracking.js:21), which 9router adds to
    prompt/total on EVERY client response (stream.js:239, stream.js:399,
    nonStreamingHandler.js:365). engrix reads only usage.total_tokens
    (engrix-agent/agent/streaming.py:1392-1396), so its ledger gained 2,000
    tokens per call. Cross-check: the same prompt measured 2,096 through
    9router and 123 over the raw path.
  * storing a chars/4 estimate as if it were real usage. We still estimate (so
    the dashboard is not zero) but tagged usage_source='estimated' so it can be
    filtered out instead of mixed into the numbers.
"""
from __future__ import annotations

import json
from typing import Any

FIELDS = ("prompt", "completion", "reasoning", "cached", "cache_creation", "total")

# Field yang jawab "call ini beneran makan akun atau gak", satuannya vendor sendiri.
# Nama field dicocokin secara generik: `original_credits` itu istilah sebagian vendor
# buat harga sebelum diskon, dan `credits_original` cara kita nyimpen dia.
_SIGNAL_KEYS = {
    "credits": ("credits",),
    "credits_original": ("original_credits", "credits_original"),
}


def _int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def extract(chunk: Any) -> dict[str, int] | None:
    """Pull usage out of one OpenAI-shaped/generic chunk. None when there is none."""
    if not isinstance(chunk, dict):
        return None
    raw = chunk.get("usage")
    if not isinstance(raw, dict):
        return None
    details = raw.get("completion_tokens_details") or {}
    prompt_details = raw.get("prompt_tokens_details") or {}
    prompt = _int(raw.get("prompt_tokens", raw.get("input_tokens", raw.get("prompt"))))
    completion = _int(raw.get("completion_tokens", raw.get("output_tokens", raw.get("completion"))))
    if not any((prompt, completion, raw.get("total_tokens"))):
        return None
    cached = _int(prompt_details.get("cached_tokens",
                                     raw.get("cached_tokens", raw.get("cache_read_input_tokens"))))
    cache_creation = _int(prompt_details.get("cache_creation_input_tokens",
                                             raw.get("cache_creation_input_tokens")))
    # Konvensi Anthropic: input_tokens TIDAK termasuk cache_read/cache_creation,
    # sedangkan `prompt` kanonik kita (konvensi OpenAI, lihat canonicalize())
    # HARUS sudah mencakup keduanya. Tanpa lipatan ini satu call ~20K token
    # yang kebaca cache tercatat puluhan token di ledger -- usage_daily dan
    # cost_usd jadi understate ~99% (TASK-50 bug #1). Lengan OpenAI murni
    # (prompt_tokens) tidak tersentuh, jadi guard cached<=prompt tetap valid.
    if "input_tokens" in raw and "prompt_tokens" not in raw:
        prompt += cached + cache_creation
    usage = {
        "prompt": prompt,
        "completion": completion,
        "reasoning": _int(details.get("reasoning_tokens")),
        "cached": cached,
        "cache_creation": cache_creation,
        "total": _int(raw.get("total_tokens")),
    }
    if not usage["total"]:
        usage["total"] = usage["prompt"] + usage["completion"]
    return usage


def canonicalize(usage: dict[str, Any] | None) -> dict[str, int]:
    """
    Normalise and fold. prompt is assumed to already include cached (the OpenAI
    convention, and 9router's own assumption in providers/pricing.js:468-470).
    Idempotent: accepts the raw OpenAI shape or our canonical shape, always
    returns canonical, and never counts cached twice.

    """
    if not isinstance(usage, dict):
        return {key: 0 for key in FIELDS}
    if any(key in usage for key in FIELDS):
        out = {key: _int(usage.get(key)) for key in FIELDS}
    else:
        out = extract({"usage": usage}) or {key: 0 for key in FIELDS}
    out["total"] = out["total"] or (out["prompt"] + out["completion"])
    # Guard non-negatif: cached tidak boleh lebih besar dari prompt.
    out["cached"] = min(out["cached"], out["prompt"])
    return _clamp_total(out)


def _clamp_total(out: dict[str, int]) -> dict[str, int]:
    """
    total can never be smaller than prompt+completion.

    Anthropic (and Qoder) split usage across frames: message_start carries the input
    count, message_delta the output count. A per-frame `total` therefore gets computed
    from partial information, and a field-wise max would keep that too-low number.
    Re-clamping after the merge is what makes the sum honest.
    """
    out["total"] = max(out.get("total", 0), out.get("prompt", 0) + out.get("completion", 0))
    return out


def merge(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, int]:
    """Field-wise max. Anthropic/Qoder split usage across frames (usageTracking.js:321-335), so summing would be wrong."""
    base = canonicalize(existing)
    other = canonicalize(incoming)
    if not any(other.values()):
        return base
    return _clamp_total({key: max(base[key], other[key]) for key in FIELDS})


def vendor_signals(usage: Any) -> dict[str, Any]:
    """
    Pull the cost-to-the-account fields out of one vendor usage block.

    `billable=None` means the vendor never said, which is NOT the same as False:
    the ledger column has to stay honest or the burn-rate report becomes fiction.
    Credits stay in the vendor's own unit and are never converted to USD here
    (ADR-0001) - pricing.cost_for() is the only place USD is computed.
    """
    if not isinstance(usage, dict):
        return {}
    out: dict[str, Any] = {}
    if isinstance(usage.get("billable"), bool):
        out["billable"] = usage["billable"]
    for target, aliases in _SIGNAL_KEYS.items():
        for alias in aliases:
            value = usage.get(alias)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[target] = float(value)
                break
    return out


def merge_signals(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the last value the vendor sent per key: a stream has one final usage frame."""
    out = dict(existing or {})
    for key, value in (incoming or {}).items():
        if value is not None:
            out[key] = value
    return out


def estimate_from_body(body: dict[str, Any]) -> dict[str, int]:
    """chars/4 heuristic, same as usageTracking.js:341-363, used ONLY when the upstream sent no usage at all."""
    try:
        serialized = json.dumps(body, ensure_ascii=False)
    except (TypeError, ValueError):
        serialized = str(body)
    prompt = max(1, -(-len(serialized) // 4))
    return {"prompt": prompt, "completion": 0, "reasoning": 0, "cached": 0,
            "cache_creation": 0, "total": prompt}


def estimate_completion(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def with_estimated_completion(usage: dict[str, int], completion: int) -> dict[str, int]:
    out = dict(usage)
    out["completion"] = max(out.get("completion", 0), completion)
    out["total"] = out["prompt"] + out["completion"]
    return out


def as_client_usage_payload(usage: dict[str, Any], *, include_details: bool = True) -> dict[str, Any]:
    """The OpenAI shape for the final chunk."""
    clean = canonicalize(usage)
    payload = {
        "prompt_tokens": clean["prompt"],
        "completion_tokens": clean["completion"],
        "total_tokens": clean["total"],
    }
    if include_details and (clean["reasoning"] or clean["cached"] or clean["cache_creation"]):
        details: dict[str, int] = {}
        if clean["reasoning"]:
            details["reasoning_tokens"] = clean["reasoning"]
        payload["completion_tokens_details"] = details
        prompt_details: dict[str, int] = {}
        if clean["cached"]:
            prompt_details["cached_tokens"] = clean["cached"]
        if clean["cache_creation"]:
            prompt_details["cache_creation_input_tokens"] = clean["cache_creation"]
        if prompt_details:
            payload["prompt_tokens_details"] = prompt_details
    return payload


def has_any(usage: dict[str, Any] | None) -> bool:
    return any(canonicalize(usage).values())
