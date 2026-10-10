"""Connection selection strategy (order + pick + record).

Copied from src/sse/services/auth.js:139-196. Worth noting about 9router's
"round-robin", because its name misleads:
  * global default = `fill-first`: the connection with the smallest priority number
    ALWAYS wins (auth.js:195, `availableConnections[0]` already sorted by priority).
  * mode `"round-robin"` = sticky-LRU: it stays on the most recently used account
    while consecutive_use_count < sticky_round_robin_limit (default 3), and only
    then moves to the stalest one (auth.js:154-192).
  * the owner on the VPS set global round-robin + sticky_limit=1, which in that
    implementation = pure RR (sticky for a single request).
We keep the three behaviours (fill-first, round-robin, least-recent) so strategies
can be compared, and sticky_limit remains adjustable.
"""
from __future__ import annotations

from typing import Any

from engrix_router.storage.sqlite import now_ms, transaction
from engrix_router.core import logs as applog
from engrix_router.accounts import connections
from engrix_router.storage import settings
from engrix_router.core.types import Candidate, ProviderDef

STRATEGY_FILL_FIRST = "fill-first"
STRATEGY_ROUND_ROBIN = "round-robin"
STRATEGY_LEAST_RECENT = "least-recent"
ALL_STRATEGIES = (STRATEGY_FILL_FIRST, STRATEGY_ROUND_ROBIN, STRATEGY_LEAST_RECENT)


def order(candidates: list[Candidate], *, strategy: str, sticky_limit: int,
          ts: int | None = None) -> list[Candidate]:
    moment = now_ms() if ts is None else ts
    if strategy == STRATEGY_FILL_FIRST:
        return sorted(candidates, key=lambda c: (c.priority, c.credentials.connection_id))
    if strategy == STRATEGY_LEAST_RECENT:
        return sorted(candidates, key=lambda c: (c.last_used_ms if c.last_used_ms else 0,
                                                 c.priority))
    # round-robin: kandidat = yang paling baru dipakai, kecuali sudah lewat sticky limit
    by_recency = sorted(
        candidates,
        key=lambda c: (-(c.last_used_ms or 0), c.priority, c.credentials.connection_id),
    )
    current = by_recency[0] if by_recency else None
    if current and current.last_used_ms and current.consecutive_use_count < max(1, sticky_limit):
        return [current, *by_recency[1:]]
    by_age = sorted(
        candidates,
        key=lambda c: (c.last_used_ms if c.last_used_ms else 0, c.priority, c.credentials.connection_id),
    )
    return by_age


def select_provider_plan(provider: str) -> dict[str, Any]:
    plan = settings.strategy_for(provider)
    plan["provider"] = provider
    return plan


def pick(definition: ProviderDef, model: str, *, exclude: set[str] | None = None,
         ts: int | None = None) -> tuple[Candidate | None, list[dict[str, Any]], dict[str, Any]]:
    """Returns (candidate, skip reasons, plan-in-use). Writes no state.

    This is the ONLY place that decides which candidate gets used; accounts only
    supplies the list + health state (rule: one decision, one owner).
    """
    plan = select_provider_plan(definition.id)
    candidates, skipped = connections.candidates(definition, model, exclude=exclude, ts=ts)
    ordered = order(candidates, strategy=plan["strategy"], sticky_limit=plan["sticky_limit"], ts=ts)
    chosen = ordered[0] if ordered else None
    plan["candidates"] = len(ordered)
    plan["strategy_effective"] = plan["strategy"]
    return chosen, skipped, plan


def next_opening_ms(skipped: list[dict[str, Any]], *, ts: int | None = None) -> int | None:
    """Earliest moment ANY of the locked connections frees up (9router auth.js:114-133).

    Without it a fully-locked provider answers with our generic default Retry-After
    while the locks still have minutes to run, so a client holding a retry ladder
    hammers it: measured 2026-10-10, engrix-agent retried 9 times in 31 seconds
    against `all_locked`. On vendors whose anti-abuse edge flags the CLIENT rather
    than the request, that hammering is exactly what extends the block.

    """
    moment = now_ms() if ts is None else ts
    openings = [int(item["until_ms"]) for item in skipped
                if item.get("until_ms") and int(item["until_ms"]) > moment]
    return min(openings) if openings else None


def mark_selected(candidate: Candidate, *, sticky: bool) -> None:
    """Record the selection BEFORE sending (not after), like 9router, which writes
    lastUsedAt/consecutiveUseCount at selection time (auth.js:172-192).

    sticky=True  -> stays on the same account: the counter goes up.
    sticky=False -> we move to another account: the counter resets to 1.
    """
    with transaction() as db:
        if sticky:
            db.execute(
                "UPDATE connection_health SET last_used_at=?, consecutive_use_count="
                " consecutive_use_count + 1, updated_at=? WHERE connection_id=?",
                (now_ms(), now_ms(), candidate.credentials.connection_id),
            )
        else:
            db.execute(
                "UPDATE connection_health SET last_used_at=?, consecutive_use_count=1, updated_at=?"
                " WHERE connection_id=?",
                (now_ms(), now_ms(), candidate.credentials.connection_id),
            )


def describe(candidate: Candidate | None) -> str:
    if candidate is None:
        return "none"
    return f"{candidate.provider_def.id}:{candidate.label}"


def log_plan(plan: dict[str, Any], chosen: Candidate | None, skipped: list[dict[str, Any]],
             *, rid: str | None = None) -> None:
    detail = f"strategy={plan['strategy_effective']} candidates={plan['candidates']} pick={describe(chosen)}"
    if skipped:
        detail += f" skip={len(skipped)} ({skipped[0]['reason']})"
    applog.debug(applog.NS_ROUTE, detail, rid=rid)
