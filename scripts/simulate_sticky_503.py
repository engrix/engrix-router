"""Before/after simulation of the "89 consecutive 503s on one account" pattern.

Mimics the account-selection loop in pipeline/runner.py::_dispatch using the real
functions (accounts.candidates + routing.selector.pick + accounts.health.register_error),
against state shaped like the 2026-10-25 production incident:

  account A  priority 1  -- BROKEN at the vendor (always 503 upstream_unavailable)
  account B  priority 2  -- healthy
  account C  priority 3  -- healthy

Strategy = fill-first (the global default; qoder has no override in the DB).
Reported: how many attempts land on A during a 15 minute window, and how many
requests are served.

Usage:
    python simulate_sticky_503.py before    # flat transient TTL (old behaviour)
    python simulate_sticky_503.py after     # escalated transient TTL (this patch)
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TMP = Path(tempfile.mkdtemp(prefix="engrix-sim-"))
os.environ["EROUTER_DATA_DIR"] = str(TMP)
os.environ["EROUTER_DB_PATH"] = str(TMP / "sim.sqlite3")
os.environ["EROUTER_LOG_DIR"] = str(TMP / "logs")
os.environ["EROUTER_LOG_FILE"] = "false"
os.environ["EROUTER_LOG_LEVEL"] = "error"
os.environ["EROUTER_REQUIRE_CLIENT_KEY"] = "false"
os.environ["EROUTER_ADMIN_TOKEN"] = "sim-token"
sys.path.insert(0, str(SRC))

from engrix_router.accounts import connections, health  # noqa: E402
from engrix_router.core import config, errors  # noqa: E402
from engrix_router.core.types import ProviderDef  # noqa: E402
from engrix_router.routing import selector  # noqa: E402
from engrix_router.storage import settings, sqlite as db  # noqa: E402

MODEL = "qfmodel"
PROVIDER = "qoder"
WINDOW_MS = 15 * 60 * 1000
REQUEST_INTERVAL_MS = 6_000  # ~150 request dalam 15 menit (setara window log)
START = 1_800_000_000_000

# Jam simulasi. mark_selected() dan is_available() memakai now_ms() milik
# modulnya sendiri, jadi keduanya diarahkan ke jam ini supaya seluruh loop
# konsisten dengan satu garis waktu.
_CLOCK = {"t": START}


def _freeze_clock() -> None:
    selector.now_ms = lambda: _CLOCK["t"]
    health.now_ms = lambda: _CLOCK["t"]



def build(mode: str) -> None:
    config.ensure_runtime_dirs()
    db.init_schema(force=True)
    # Nilai yang TERPASANG di DB produksi owner saat insiden.
    settings.set_setting("health.cooldown_transient_ms", 2_000, actor="sim")
    settings.set_setting("health.backoff_max_ms", 300_000, actor="sim")
    settings.set_setting("health.backoff_max_level", 15, actor="sim")
    settings.set_setting("routing.strategy", "fill-first", actor="sim")
    if mode == "before":
        # Matikan eskalasi: tiru kode lama dengan mengosongkan daftar kelas.
        health.ESCALATING_CLASSES = frozenset()
    for index, name in enumerate(("A-broken", "B-healthy", "C-healthy"), start=1):
        connections.create(provider=PROVIDER, name=name, api_key=f"key-{name}",
                           priority=index)


def run() -> dict:
    definition = ProviderDef(id=PROVIDER, category="agentic", transport="sse",
                            display_name="Qoder", models=[])
    plan = selector.select_provider_plan(PROVIDER)
    hits = {"A-broken": 0, "B-healthy": 0, "C-healthy": 0}
    first_pick_is_a = 0
    requests = 0
    moment = START
    while moment < START + WINDOW_MS:
        requests += 1
        exclude: set[str] = set()
        served = False
        for _attempt in range(3):  # max_connections_per_request cukup untuk 3 akun
            chosen, _skipped, _plan = selector.pick(definition, MODEL,
                                                    exclude=exclude, ts=moment)
            if chosen is None:
                break
            name = chosen.label
            if _attempt == 0 and name == "A-broken":
                first_pick_is_a += 1
            hits[name] = hits.get(name, 0) + 1
            selector.mark_selected(chosen)
            if name == "A-broken":
                health.register_error(chosen.credentials.connection_id, MODEL,
                                      errors.Classified(
                                          errors.CLASS_UPSTREAM_UNAVAILABLE,
                                          errors.policy_of(errors.CLASS_UPSTREAM_UNAVAILABLE),
                                          "upstream 500"),
                                      ts=moment)
                exclude.add(chosen.credentials.connection_id)
                continue
            health.register_success(chosen.credentials.connection_id, MODEL, ts=moment)
            served = True
            break
        if not served:
            pass
        _CLOCK["t"] = moment
        moment += REQUEST_INTERVAL_MS
    return {"requests": requests, "hits": hits, "first_pick_a": first_pick_is_a}


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "after").lower()
    if mode not in {"before", "after"}:
        print("usage: simulate_sticky_503.py before|after")
        return 2
    build(mode)
    _freeze_clock()
    result = run()
    print(f"mode               : {mode}")
    print(f"strategy           : {settings.strategy_for(PROVIDER)}")
    print(f"transient cooldown : {settings.get_int('health.cooldown_transient_ms')} ms")
    print(f"escalating classes : {sorted(health.ESCALATING_CLASSES) or '(disabled = old code)'}")
    print(f"requests in window : {result['requests']}")
    print(f"attempts on A      : {result['hits']['A-broken']}")
    print(f"attempts on B      : {result['hits']['B-healthy']}")
    print(f"attempts on C      : {result['hits']['C-healthy']}")
    ratio = result["hits"]["A-broken"] / max(1, result["requests"])
    print(f"A hits per request : {ratio:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
