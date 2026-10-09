"""Tests for the pieces that are not a provider: limiter, background job, discovery rules.

Each one exists because the corresponding failure is silent:
  * a limiter that never fires means one client can burn a whole vendor's quota;
  * a scheduler job that raises once must not stop syncing forever;
  * a template file that became routable would put `myvendor` in the model list;
  * dry-run that actually calls upstream would spend the credits it exists to save.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil

import pytest

from engrix_router.accounts import connections, limits
from engrix_router.core import config
from engrix_router.core.types import (
    ProviderDef, QuotaReading, TransportSpec, UpstreamError, UsageSpec,
)
from engrix_router.hooks import ratelimit
from engrix_router.providers import registry
from engrix_router.services import quota_sync
from engrix_router.storage import settings
from engrix_router.subscribers import trace as trace_mod


@pytest.fixture(autouse=True)
def _clean_limiter():
    ratelimit.reset()
    yield
    ratelimit.reset()


# ── 1. short-window limiter ──────────────────────────────────────────────────
def test_limiter_opens_then_denies_inside_the_window():
    settings.set_setting("ratelimit.requests_per_minute_per_key", 2, actor="test")
    assert ratelimit.check_admission(api_key_id="k1", provider="openai")["allowed"]
    assert ratelimit.check_admission(api_key_id="k1", provider="openai")["allowed"]
    denied = ratelimit.check_admission(api_key_id="k1", provider="openai")
    assert not denied["allowed"]
    assert denied["blocked_by"][0]["scope"] == "api_key:k1"
    assert ratelimit.retry_after(0.0) >= 1


def test_limiter_windows_are_independent_per_key_and_provider():
    settings.set_setting("ratelimit.requests_per_minute_per_key", 1, actor="test")
    settings.set_setting("ratelimit.requests_per_minute_per_provider", 3, actor="test")
    assert ratelimit.check_admission(api_key_id="a", provider="openai")["allowed"]
    assert ratelimit.check_admission(api_key_id="b", provider="openai")["allowed"]
    assert not ratelimit.check_admission(api_key_id="a", provider="openai")["allowed"]


def test_limiter_is_off_by_default_and_never_blocks():
    """The DEFAULT is what a fresh install runs with, so it must be provably disabled."""
    assert settings.DEFAULTS["ratelimit.requests_per_minute_per_key"][0] == 0
    assert settings.DEFAULTS["ratelimit.requests_per_minute_per_provider"][0] == 0
    settings.set_setting("ratelimit.requests_per_minute_per_key", 0, actor="test")
    settings.set_setting("ratelimit.requests_per_minute_per_provider", 0, actor="test")
    for _ in range(50):
        assert ratelimit.check_admission(api_key_id="k", provider="openai")["allowed"]


def test_admission_denies_before_any_upstream_work(monkeypatch):
    """A limiter hit must not cost a vendor call -- that is the point of admission."""
    from engrix_router.pipeline import runner

    settings.set_setting("ratelimit.requests_per_minute_per_provider", 1, actor="test")
    calls = []

    async def fake_complete(self, request):
        calls.append(request.url)
        return {"id": "x", "object": "chat.completion", "choices": [{"index": 0, "message":
                    {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}

    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete", fake_complete)
    connections.create(provider="openai", name="limiter-account", api_key="sk-limit")
    body = {"model": "openai/gpt-test", "messages": [{"role": "user", "content": "hi"}]}

    async def drive():
        first = await runner.run_chat(body)
        with pytest.raises(runner.RequestRejected) as raised:
            await runner.run_chat(body)
        return first, raised.value

    first, rejected = asyncio.run(drive())
    assert first["choices"][0]["message"]["content"] == "OK"
    assert rejected.classified.error_class == "rate_limited"
    assert rejected.classified.client_status == 429
    assert len(calls) == 1, "the denied request still reached the upstream"


# ── 2. quota sync job ────────────────────────────────────────────────────────
class _QuotaProvider:
    def __init__(self, definition):
        self.definition = definition
        self.calls = 0

    async def fetch_quota(self, creds):
        self.calls += 1
        return [QuotaReading(scope="userQuota", used=10, total=100, remaining=90)]


def _quota_definition():
    return ProviderDef(id="quotatest", category="apikey",
                       transport=TransportSpec(base_url="https://q.example.invalid/v1",
                                               usage=UsageSpec(url="https://q.example.invalid/quota")),
                       features=frozenset({"usage"}))


async def test_sync_quota_once_writes_a_snapshot_for_capable_connections(monkeypatch):
    definition = _quota_definition()
    provider = _QuotaProvider(definition)
    row = connections.create(provider="quotatest", name="acct", api_key="sk-q")
    monkeypatch.setattr(registry, "definitions", lambda include_nodes=True: [definition])
    monkeypatch.setattr(registry, "get_provider", lambda prefix: provider)

    result = await quota_sync.sync_quota_once()

    assert result["synced"] == 1 and provider.calls == 1
    assert limits.history("quotatest"), "the reading was never stored"
    assert limits.latest_for_provider("quotatest")[0]["remaining"] == 90
    connections.delete(row["id"])


async def test_sync_skips_providers_without_a_usage_spec(monkeypatch):
    definition = ProviderDef(id="nousage", category="apikey",
                             transport=TransportSpec(base_url="https://n.example.invalid/v1"))
    row = connections.create(provider="nousage", name="acct", api_key="sk-n")
    monkeypatch.setattr(registry, "definitions", lambda include_nodes=True: [definition])
    result = await quota_sync.sync_quota_once()
    assert result["synced"] == 0
    connections.delete(row["id"])


async def test_a_failing_sync_does_not_kill_the_loop(monkeypatch):
    stop = asyncio.Event()
    ticks = []

    async def exploding_once():
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("vendor is having a day")
        stop.set()
        return {"synced": 0, "detail": [], "rows": []}

    monkeypatch.setattr(quota_sync, "sync_quota_once", exploding_once)
    await quota_sync.run_forever(stop=stop, sleep_s=5)
    assert len(ticks) == 2, "one failure stopped the scheduler"


# ── 7. the file log sink the env vars promise ────────────────────────────────
def test_log_file_env_vars_actually_write_and_rotate():
    """
    EROUTER_LOG_FILE* used to be read into config and then ignored.

    Run in a subprocess: `configure()` is process-singleton, so testing it in-process
    would leak handlers into every later test.
    """
    import subprocess
    import sys

    root = pathlib.Path(config.DATA_DIR) / "logsink"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    env = {**os.environ,
           "PYTHONPATH": str(pathlib.Path(config.PACKAGE_DIR).parent),
           "PYTHONIOENCODING": "utf-8",
           "EROUTER_LOG_DIR": str(root),
           "EROUTER_LOG_FILE": "true",
           "EROUTER_LOG_FILE_MAX_BYTES": "2000",
           "EROUTER_LOG_FILE_BACKUPS": "2",
           "EROUTER_LOG_LEVEL": "info"}
    code = (
        "import pathlib, sys\n"
        "sys.path.insert(0, PKG)\n"
        "from engrix_router.core import logs\n"
        "for i in range(60):\n"
        "    logs.info(logs.NS_APP, 'line %d ' % i + 'x' * 80)\n"
        "print(sorted(p.name for p in pathlib.Path(DIR).iterdir()))\n"
    ).replace("PKG", repr(str(config.PACKAGE_DIR.parent))).replace("DIR", repr(str(root)))
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-400:]
    files = eval(done.stdout.strip().splitlines()[-1])
    assert "engrix-router.log" in files, files
    backups = [f for f in files if f.startswith("engrix-router.log.")]
    assert backups, f"maxBytes=2000 with 60 lines must rotate, got {files}"
    assert len(backups) <= 2, f"backupCount=2 dilanggar: {files}"
    content = (root / "engrix-router.log").read_text(encoding="utf-8")
    assert "line " in content


# ── 8. registry hygiene ──────────────────────────────────────────────────────
def test_the_provider_template_is_never_routable():
    prefixes = {d.id for d in registry.builtin_definitions()}
    assert "myvendor" not in prefixes
    assert "_template" not in str(prefixes)


# ── 4. dry-run really does not shoot ─────────────────────────────────────────
def test_dry_run_traces_the_request_without_calling_upstream(monkeypatch):
    from engrix_router.pipeline import runner

    monkeypatch.setattr(config, "DRY_RUN", True)

    async def explode(self, request):
        raise AssertionError("dry run must never reach the upstream")

    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete", explode)
    row = connections.create(provider="openai", name="dry", api_key="sk-dry")

    async def drive():
        return await runner.run_chat({"model": "openai/gpt-test",
                                      "messages": [{"role": "user", "content": "hi"}]})

    result = asyncio.run(drive())
    assert result["engrix_dry_run"] is True
    stored = trace_mod.get(result["id"].removeprefix("chatcmpl-"))
    assert stored["status"] == trace_mod.STATUS_DRY_RUN
    stages = [stage["name"] for stage in stored["stages"]]
    assert "provider_out" in stages and "dry_run" in stages
    assert "upstream_in" not in stages
    body = json.loads(next(s for s in stored["stages"] if s["name"] == "provider_out")["payload_json"])
    assert body["body"]["messages"][0]["content"] == "hi"
    connections.delete(row["id"])


# ── 5. failover across connections ───────────────────────────────────────────
def test_a_dead_connection_moves_the_request_to_the_next_one(monkeypatch):
    """The retry ladder lives in the runner (not the provider) -- proven with two accounts."""
    from engrix_router.accounts import health
    from engrix_router.pipeline import runner

    attempts: list[str] = []

    async def first_fails(self, request):
        attempts.append(str(request.headers.get("authorization") or ""))
        if len(attempts) == 1:
            raise UpstreamError("upstream having a day", status=503)
        return {"id": "c", "object": "chat.completion",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "FROM-2"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}

    monkeypatch.setattr(registry.get_provider("openai").__class__, "complete", first_fails)
    dead = connections.create(provider="openai", name="dead", api_key="sk-dead", priority=1)
    alive = connections.create(provider="openai", name="alive", api_key="sk-alive", priority=2)

    result = asyncio.run(runner.run_chat({"model": "openai/gpt-test",
                                          "messages": [{"role": "user", "content": "hi"}]}))

    assert len(attempts) == 2, "the runner did not fail over"
    assert result["choices"][0]["message"]["content"] == "FROM-2"
    stored = trace_mod.get(result["id"].removeprefix("chatcmpl-"))
    assert stored["connection_id"] == alive["id"], "trace recorded the wrong account"
    assert stored["status"] == trace_mod.STATUS_OK
    health_row = health.get(dead["id"])
    assert health_row["error_class"] == "upstream_unavailable", health_row
    assert "upstream having a day" in str(health_row["last_error"])
    connections.delete(dead["id"])
    connections.delete(alive["id"])


# ── 6. live log stream ───────────────────────────────────────────────────────
def test_logs_stream_delivers_the_buffer_and_needs_admin():
    from fastapi.testclient import TestClient

    from engrix_router.core import logs as applog
    from engrix_router.web import server

    admin = {"authorization": "Bearer unit-admin-token"}
    with TestClient(server.app) as client:
        assert client.get("/api/logs/stream").status_code == 401   # admin-gated, like the rest
        applog.info(applog.NS_APP, "sentinel line for the stream test")
        with client.stream("GET", "/api/logs/stream?seconds=1", headers=admin) as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            lines = [line for line in response.iter_lines() if line.startswith("data: ")]
    payload = json.loads(lines[0][6:])
    assert payload["type"] == "init" and isinstance(payload["lines"], list)
    assert any("sentinel line" in json.dumps(entry) for entry in payload["lines"])
