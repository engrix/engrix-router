"""TASK-48 -- probe dashboard hampir mematikan satu provider.

Dua hal yang dibuktikan di sini (offline, upstream distub, nol jaringan):

  1. `probe_body()` BUKAN micro-probe. Vendor yang membaca BENTUK body (ZCode:
     405/3012 "unusual activity") ngebaca probe micro sebagai traffic bot, dan
     flag itu per-KLIEN, bukan per-request: di DB asli, agent traffic yang jam
     19:27 masih 200 jadi 3012 semua pada 19:39 setelah tiga akun di-probe
     dengan body `max_tokens: 8 / "reply with OK"` -- bentuk body-nya sama.
  2. GAGAL probe tidak boleh menggeser health fleet: satu probe yang ditolak
     jangan sampai mengunci model / membuang akun sehat dari rotasi.
"""
from __future__ import annotations

import pytest

from fastapi.testclient import TestClient

from engrix_router.accounts import connections, health
from engrix_router.core import errors
from engrix_router.core.types import UpstreamError
from engrix_router.hooks import ratelimit
from engrix_router.identity import api_keys
from engrix_router.pipeline import runner
from engrix_router.providers import anthropic as anthropic_provider
from engrix_router.providers import registry
from engrix_router.routing import selector
from engrix_router.storage.sqlite import now_ms, query
from engrix_router.web import server

REJECTING_BODY = '{"code":3012,"msg":"request has been blocked due to unusual activity.,"}'


@pytest.fixture(autouse=True)
def _clear_rate_windows():
    """Window rate limit hidup di RAM dan kebawa antar-tes satu session -- tanpa
    ini, tes kedua ke-blok admission dan bukan upstream yang diuji."""
    ratelimit.reset()
    yield
    ratelimit.reset()


def test_probe_body_is_not_micro_shaped():
    body = registry.get_provider("anthropic").probe_body("anthropic/claude-x")
    assert body["max_tokens"] >= 32, "micro max_tokens dibaca edge sebagai probe"
    assert body["stream"] is False
    assert len(body["messages"][0]["content"]) >= 20, "pesan 2 kata = bentuk probe"
    assert body["model"] == "anthropic/claude-x"


def _rejecting(monkeypatch):
    async def fake_complete(self, request):
        raise UpstreamError(REJECTING_BODY, status=405)

    monkeypatch.setattr(anthropic_provider.AnthropicProvider, "complete", fake_complete)


def _run(record_health: bool):
    """Panggil probe lewat pipeline dan pastikan yang ditolak itu UPSTREAM,
    bukan admission kita (rate limit / budget) -- assert class-nya."""
    chat_body = registry.get_provider("anthropic").probe_body("anthropic/claude-x")
    with pytest.raises(runner.RequestRejected) as exc:
        import asyncio
        asyncio.run(runner.run_chat(chat_body, lane="debug", record_health=record_health))
    assert exc.value.classified.error_class == errors.CLASS_ANTI_ABUSE


def test_probe_failure_does_not_touch_fleet_health(monkeypatch):
    conn = connections.create(provider="anthropic", name="probe-acct", api_key="sk-test")
    _rejecting(monkeypatch)
    _run(record_health=False)

    assert not query("SELECT * FROM model_locks WHERE connection_id = ?", (conn["id"],))
    state = health.get(conn["id"])
    assert state["test_status"] != "unavailable"
    assert state["error_class"] is None


def test_real_failure_still_locks_the_model(monkeypatch):
    """Kontra buat yang di atas: flag-nya jangan sampai diam-diam mematikan
    pencatatan health buat traffic klien sungguhan."""
    conn = connections.create(provider="anthropic", name="client-acct", api_key="sk-test")
    _rejecting(monkeypatch)
    _run(record_health=True)

    locks = query("SELECT model, reason FROM model_locks WHERE connection_id = ?", (conn["id"],))
    assert [row["model"] for row in locks] == ["claude-x"]
    assert "anti_abuse_shape" in locks[0]["reason"]


# ── port dari 9router: Retry-After = kunci paling cepat buka ──────────────────
def test_next_opening_ms_picks_the_earliest_future_lock():
    now = now_ms()
    skipped = [{"connection_id": "a", "until_ms": now + 90_000},
               {"connection_id": "b", "until_ms": now + 30_000},
               {"connection_id": "c", "until_ms": now - 1_000},
               {"connection_id": "d", "reason": "already tried in this request"}]
    assert selector.next_opening_ms(skipped, ts=now) == now + 30_000
    assert selector.next_opening_ms([], ts=now) is None


def test_all_locked_menghormati_jendela_lock_bukan_default_15s():
    """9router auth.js:114-133. Default Retry-After kita (15s) << jendela lock,
    jadi klien dengan retry ladder meng-hammer provider yang lagi terkunci
    semua -- dan pada vendor yang flag per-klien, hammer itu yang memperpanjang
    block-nya."""
    conn = connections.create(provider="anthropic", name="locked-acct", api_key="sk-test")
    classified = errors.classify(status=405, text=REJECTING_BODY)
    health.register_error(conn["id"], "claude-x", classified)

    key = api_keys.create_key("task48-retry-after")
    with TestClient(server.app) as client:
        response = client.post("/v1/chat/completions",
                               headers={"authorization": f"Bearer {key['key']}"},
                               json={"model": "anthropic/claude-x", "max_tokens": 8,
                                     "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 503
    retry_after = int(response.headers["retry-after"])
    assert 100 <= retry_after <= 120, f"harusnya jendela lock (~120s), bukan default 15s: {retry_after}"
    body = response.json()["error"]
    assert body["code"] == errors.CLASS_ALL_LOCKED
    assert body["retry_after"] == retry_after
