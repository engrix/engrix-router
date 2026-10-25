"""Before/after proof for the global failover contract, using the REAL selector.

Two things are proven, both offline (no network, no production DB):

  1. ROTATION ("sticky 1"): with 5 healthy accounts, 20 consecutive requests must
     spread evenly and NO two consecutive requests may land on the same account.
     That is the definition of round-robin sticky_limit=1.
  2. FAILOVER WITHIN ONE REQUEST: a request that gets a 503 on the first account
     must continue to the second account inside the SAME request, not die.

Usage:  python scripts/simulate_global_failover.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from engrix_router.core import errors                                    # noqa: E402
from engrix_router.core.types import Candidate, Credentials, ProviderDef, TransportSpec  # noqa: E402
from engrix_router.routing import selector                               # noqa: E402

DEFINITION = ProviderDef(id="sim", category="apikey",
                         transport=TransportSpec(base_url="https://example.invalid"))


def candidate(cid: str, priority: int, last_used: int | None, count: int) -> Candidate:
    return Candidate(
        credentials=Credentials(connection_id=cid, provider="sim", auth_type="apikey",
                                name=cid, token="x"),
        provider_def=DEFINITION, priority=priority, test_status="active",
        last_used_ms=last_used, consecutive_use_count=count, rate_limited_until_ms=None,
    )


def rotation_report(strategy: str, sticky_limit: int) -> dict[str, object]:
    """Simulate 20 requests over 5 accounts and describe where they landed."""
    state = {f"acc{i}": {"last_used": None, "count": 0} for i in range(1, 6)}
    order_seen: list[str] = []

    for tick in range(1, 21):
        pool = [candidate(cid, int(cid[3:]), st["last_used"], st["count"])
                for cid, st in state.items()]
        ordered = selector.order(pool, strategy=strategy, sticky_limit=sticky_limit,
                                 ts=tick * 1000)
        chosen = ordered[0].credentials.connection_id
        order_seen.append(chosen)
        # selector.record() memakai aturan yang sama: sticky menaikkan hitungan,
        # non-sticky mereset ke 1.
        state[chosen]["last_used"] = tick * 1000
        state[chosen]["count"] = state[chosen]["count"] + 1 if (
            len(order_seen) > 1 and order_seen[-2] == chosen) else 1

    repeats = sum(1 for a, b in zip(order_seen, order_seen[1:]) if a == b)
    spread = {cid: order_seen.count(cid) for cid in state}
    return {"order": order_seen, "consecutive_repeats": repeats, "spread": spread}


def failover_report() -> dict[str, object]:
    """One request: acc1 answers 503, acc2 answers 503, acc3 answers 200."""
    outcomes = {"acc1": 503, "acc2": 503, "acc3": 200}
    excluded: set[str] = set()
    tried: list[str] = []
    cap = 100

    while True:
        pool = [candidate(cid, int(cid[3:]), None, 0)
                for cid in ("acc1", "acc2", "acc3") if cid not in excluded]
        if not pool:
            return {"served_by": None, "tried": tried, "note": "no candidate left"}
        chosen = selector.order(pool, strategy=selector.STRATEGY_ROUND_ROBIN,
                                sticky_limit=1)[0].credentials.connection_id
        tried.append(chosen)
        status = outcomes[chosen]
        if status == 200:
            return {"served_by": chosen, "tried": tried, "note": "success"}
        classified = errors.classify(status=status, text=f"upstream {status}")
        excluded.add(chosen)
        if not classified.policy.retry_internally:
            return {"served_by": None, "tried": tried,
                    "note": f"stopped: {classified.error_class} not retryable"}
        if len(tried) >= cap:
            return {"served_by": None, "tried": tried, "note": "attempt cap"}


def main() -> int:
    print("=== 1. ROTATION: 20 requests over 5 healthy accounts ===")
    for label, strategy, limit in (("fill-first (old default)", "fill-first", 3),
                                   ("round-robin sticky=3 (old zcode)", "round-robin", 3),
                                   ("round-robin sticky=1 (CURRENT)", "round-robin", 1)):
        report = rotation_report(strategy, limit)
        print(f"\n{label}")
        print(f"  order   : {' '.join(a.replace('acc', '') for a in report['order'])}")
        print(f"  spread  : {report['spread']}")
        print(f"  consecutive requests on the same account: {report['consecutive_repeats']}")

    print("\n=== 2. FAILOVER inside ONE request (acc1=503, acc2=503, acc3=200) ===")
    result = failover_report()
    print(f"  accounts tried : {result['tried']}")
    print(f"  served by      : {result['served_by']}")
    print(f"  note           : {result['note']}")

    print("\n=== 3. Classes that must NOT fail over (not an account problem) ===")
    for cls in (errors.CLASS_CLIENT_NO_KEY, errors.CLASS_BUDGET_EXCEEDED,
                errors.CLASS_ALL_LOCKED, errors.CLASS_ABORTED,
                errors.CLASS_STREAM_STALLED, errors.CLASS_PROTOCOL_DRIFT):
        print(f"  {cls:18s} retry_internally={errors.policy_of(cls).retry_internally}")

    print("\n=== 4. Classes that MUST fail over (they come from upstream) ===")
    for cls in (errors.CLASS_RATE_LIMIT, errors.CLASS_QUOTA_DAILY,
                errors.CLASS_UPSTREAM_UNAVAILABLE, errors.CLASS_UPSTREAM_TIMEOUT,
                errors.CLASS_UNKNOWN, errors.CLASS_CREDENTIAL_DEAD,
                errors.CLASS_ACCOUNT_RESTRICTED,
                errors.CLASS_ANTI_ABUSE, errors.CLASS_NO_PLAN,
                errors.CLASS_PRICING_BLOCKED, errors.CLASS_CLIENT_BAD_REQUEST,
                errors.CLASS_MODEL_NOT_FOUND, errors.CLASS_MODEL_CONCURRENCY,
                errors.CLASS_QUEUE_THROTTLED):
        print(f"  {cls:22s} retry_internally={errors.policy_of(cls).retry_internally}")

    ok = (result["served_by"] == "acc3"
          and rotation_report("round-robin", 1)["consecutive_repeats"] == 0)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
