"""Daily budget: reserve before sending, settle once real usage is known.

This is the part 9router does NOT have. Their router never rejects a request on
budget grounds -- every 429 that goes out is a 429 from the vendor
(src/sse/handlers/chat.js:239-244 only maps upstream). The measured consequence
on the real port: 404-model requests burned 27.5 million tokens in one day,
drained the trial of two accounts, and not one component could say "enough".

Model: a bucket per (UTC day, scope). Reserve uses the prompt estimate + headroom;
settle uses the upstream numbers. If settle < reserve, the difference goes back to
the bucket. The reset follows the UTC day change because vendor code 110 = daily
counter.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from engrix_router.storage.sqlite import date_key_utc, execute, now_ms, query_one
from engrix_router.storage import settings

SCOPE_GLOBAL = "global"


def scope_provider(provider: str) -> str:
    return f"provider:{provider}"


def scope_key(api_key_id: str | None) -> str:
    return f"key:{api_key_id}" if api_key_id else "key:anonymous"


def scope_lane(lane: str) -> str:
    return f"lane:{lane}"


def build_scopes(*, provider: str, api_key_id: str | None, lane: str = "interactive") -> list[str]:
    return [SCOPE_GLOBAL, scope_provider(provider), scope_lane(lane), scope_key(api_key_id)]


@dataclass
class Reservation:
    scopes: list[str]
    tokens: int
    requests: int

    def release_tokens(self) -> int:
        return self.tokens


def _limit_for(scope: str) -> tuple[int, int]:
    """(token_limit, request_limit), 0 = unlimited."""
    if scope == SCOPE_GLOBAL:
        return settings.get_int("budget.global_tokens_per_day"), \
            settings.get_int("budget.global_requests_per_day")
    if scope.startswith("provider:"):
        provider = scope.split(":", 1)[1]
        return int(settings.get_dict("budget.per_provider_tokens_per_day").get(provider, 0)), 0
    if scope.startswith("key:"):
        key_id = scope.split(":", 1)[1]
        return int(settings.get_dict("budget.per_key_tokens_per_day").get(key_id, 0)), 0
    return 0, 0


def ensure_bucket(scope: str, *, period: str | None = None) -> None:
    moment = now_ms()
    execute(
        "INSERT INTO budgets(period_key, scope, token_limit, req_limit, used_tokens, used_requests, updated_at)"
        " VALUES(?,?,?,?,0,0,?) ON CONFLICT(period_key, scope) DO UPDATE SET updated_at = excluded.updated_at",
        (period or date_key_utc(), scope, _limit_for(scope)[0], _limit_for(scope)[1], moment),
    )


def would_exceed(scope: str, add_tokens: int, *, period: str | None = None) -> dict[str, Any] | None:
    ensure_bucket(scope, period=period)
    limit, _req_limit = _limit_for(scope)
    row = query_one("SELECT used_tokens FROM budgets WHERE period_key = ? AND scope = ?",
                    (period or date_key_utc(), scope))
    used = int(row["used_tokens"]) if row else 0
    if limit and used + add_tokens > limit:
        return {"scope": scope, "limit": limit, "used": used, "wanted": add_tokens}
    return None


def check_admission(*, provider: str, api_key_id: str | None, est_tokens: int,
                    lane: str = "interactive") -> dict[str, Any]:
    """May we send? Writes nothing -- it only reads buckets."""
    blocked: list[dict[str, Any]] = []
    for scope in build_scopes(provider=provider, api_key_id=api_key_id, lane=lane):
        hit = would_exceed(scope, est_tokens)
        if hit:
            blocked.append(hit)
    return {"allowed": not blocked, "blocked_by": blocked, "est_tokens": est_tokens,
            "date_key": date_key_utc()}


def reserve(*, provider: str, api_key_id: str | None, est_tokens: int,
            lane: str = "interactive") -> Reservation:
    tokens = max(1, int(est_tokens) + max(0, settings.get_int("budget.reserve_headroom_tokens")))
    for scope in build_scopes(provider=provider, api_key_id=api_key_id, lane=lane):
        ensure_bucket(scope)
        execute("UPDATE budgets SET used_tokens = used_tokens + ?, used_requests = used_requests + 1,"
                " updated_at = ? WHERE period_key = ? AND scope = ?",
                (tokens, now_ms(), date_key_utc(), scope))
    return Reservation(scopes=build_scopes(provider=provider, api_key_id=api_key_id, lane=lane),
                       tokens=tokens, requests=1)


def settle(reservation: Reservation, actual_tokens: int) -> None:
    """Correct the bucket once real usage is known. The delta may be negative."""
    delta = int(actual_tokens) - reservation.tokens
    if delta == 0:
        return
    for scope in reservation.scopes:
        ensure_bucket(scope)
        execute("UPDATE budgets SET used_tokens = MAX(0, used_tokens + ?), updated_at = ?"
                " WHERE period_key = ? AND scope = ?", (delta, now_ms(), date_key_utc(), scope))


def snapshot() -> dict[str, Any]:
    from engrix_router.storage.sqlite import query

    period = date_key_utc()
    items: list[dict[str, Any]] = []
    for row in query("SELECT * FROM budgets WHERE period_key = ? ORDER BY scope", (period,)):
        item = dict(row)
        limit = int(item.get("token_limit") or 0)
        used = int(item.get("used_tokens") or 0)
        item["remaining"] = (limit - used) if limit else None
        item["used_pct"] = round(used / limit * 100, 2) if limit else None
        items.append(item)
    return {"date_key": period, "rows": items, "bucket_count": len(items)}


def set_limits(*, scope: str, token_limit: int | None = None, req_limit: int | None = None) -> dict[str, Any]:
    """Change the ceiling of a scope. Also persisted into settings so tomorrow's
    bucket inherits it (a bucket is only created once it is used)."""
    ensure_bucket(scope)
    if token_limit is not None:
        if scope == SCOPE_GLOBAL:
            settings.set_setting("budget.global_tokens_per_day", int(token_limit), actor="budget")
        elif scope.startswith("provider:"):
            table = dict(settings.get_dict("budget.per_provider_tokens_per_day"))
            table[scope.split(":", 1)[1]] = int(token_limit)
            settings.set_setting("budget.per_provider_tokens_per_day", table, actor="budget")
        elif scope.startswith("key:"):
            table = dict(settings.get_dict("budget.per_key_tokens_per_day"))
            table[scope.split(":", 1)[1]] = int(token_limit)
            settings.set_setting("budget.per_key_tokens_per_day", table, actor="budget")
        execute("UPDATE budgets SET token_limit = ?, updated_at = ? WHERE period_key = ? AND scope = ?",
                (int(token_limit), now_ms(), date_key_utc(), scope))
    if req_limit is not None and scope == SCOPE_GLOBAL:
        settings.set_setting("budget.global_requests_per_day", int(req_limit), actor="budget")
        execute("UPDATE budgets SET req_limit = ?, updated_at = ? WHERE period_key = ? AND scope = ?",
                (int(req_limit), now_ms(), date_key_utc(), scope))
    row = query_one("SELECT * FROM budgets WHERE period_key = ? AND scope = ?", (date_key_utc(), scope))
    return dict(row) if row else {}
