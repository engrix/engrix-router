"""Verify the gateway's vertical slice WITHOUT any outbound network.

Run: `python scripts/verify_slice.py`
The upstream is stubbed (provider.complete / open_stream are monkeypatched), so what is
under test is the gateway path itself: auth -> admission -> routing -> dispatch -> trace
-> usage -> rollup -> the read APIs.

The artifact data/verify_slice.json (request/trace/budget results) is the point: evidence
you can read, not just an exit code.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("EROUTER_ADMIN_TOKEN", "test-admin-token")
os.environ.setdefault("EROUTER_DATA_DIR", str(ROOT / "data" / "verify"))
os.environ.setdefault("EROUTER_DB_PATH", str(ROOT / "data" / "verify" / "verify.sqlite3"))
os.environ.setdefault("EROUTER_LOG_LEVEL", "info")

from engrix_router.storage import sqlite as db
from engrix_router.core import config

config.ensure_runtime_dirs()
if config.DB_PATH.exists():
    config.DB_PATH.unlink()
db.ensure_ready()

from fastapi.testclient import TestClient  # noqa: E402

from engrix_router.subscribers import trace
from engrix_router.accounts import health
from engrix_router.hooks import budget
from engrix_router.routing import selector
from engrix_router.identity import api_keys
from engrix_router.storage import settings
from engrix_router.transport import proxy
from engrix_router.providers import openai as openai_provider, registry  # noqa: E402
from engrix_router.web import server  # noqa: E402

# Vendor yang ngirim sinyal biaya: angka ini harus nyampe ke ledger, bukan ilang.
VENDOR_SIGNALS = {"billable": False, "credits": 0.0028057142857142855,
                  "original_credits": 0.0028057142857142855}

FAKE_CHUNKS = [
    {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "verify/chat-test",
     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "HE"}, "finish_reason": None}]},
    {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "verify/chat-test",
     "choices": [{"index": 0, "delta": {"content": "LO"}, "finish_reason": None}]},
    {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "verify/chat-test",
     "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
     "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
               "completion_tokens_details": {"reasoning_tokens": 3},
               "prompt_tokens_details": {"cached_tokens": 4}, **VENDOR_SIGNALS}},
]

PROBED = {"count": 0}


async def fake_complete(self, request):  # noqa: ANN001
    return {"id": "c1", "object": "chat.completion", "created": 2, "model": "verify/chat-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "HELLO"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


async def fake_stream(self, request):  # noqa: ANN001
    for chunk in FAKE_CHUNKS:
        yield chunk


async def fake_models(self, creds):  # noqa: ANN001
    from engrix_router.core.types import ModelSpec

    return [ModelSpec(id="chat-test", name="chat-test", context_length=200000)]


openai_provider.OpenAICompatibleProvider.complete = fake_complete
openai_provider.OpenAICompatibleProvider.open_stream = fake_stream
openai_provider.OpenAICompatibleProvider.list_models = fake_models
openai_provider.OpenAICompatibleProvider.probe = lambda self, creds, model_id=None: _probe(self, creds, model_id)


async def _probe(self, creds, model_id=None):  # noqa: ANN001
    from engrix_router.core.types import ProbeResult

    PROBED["count"] += 1
    return ProbeResult(ok=True, tier="models_list", status=200, latency_ms=3, model_probed="chat-test")


openai_provider.OpenAICompatibleProvider.probe = _probe

client = TestClient(server.app)
results = {}
failures = []


def check(name: str, condition: bool, detail: object = None) -> None:
    results[name] = {"ok": bool(condition), "detail": detail}
    if not condition:
        failures.append(f"{name}: {detail}")


with client:
    # 1) health publik
    r = client.get("/health")
    check("health_public", r.status_code == 200 and r.json()["ok"], r.json() if r.status_code != 200 else r.json())

    # 2) admin fail-closed tanpa token
    r = client.get("/api/keys")
    check("admin_requires_token", r.status_code == 401, r.status_code)

    ADMIN = {"authorization": "Bearer test-admin-token"}

    # 3) buat klien key + pastikan value cuma keluar sekali
    r = client.post("/api/keys", json={"name": "verify-client"}, headers=ADMIN)
    created = r.json()
    check("key_created", r.status_code == 201 and created.get("key", "").startswith("egk-live-"), created)
    r = client.get("/api/keys", headers=ADMIN)
    listed = r.json()
    check("key_never_returned_in_list",
          all(item.get("key") is None and "key_prefix" in item for item in listed), listed)

    # 4) /v1 tanpa key = 401, dengan key = lolos
    r = client.get("/v1/models")
    check("v1_requires_client_key", r.status_code == 401, r.status_code)
    CLIENT_HDR = {"authorization": f"Bearer {created['key']}"}
    r = client.get("/v1/models", headers=CLIENT_HDR)
    check("v1_models_accepts_client_key", r.status_code == 200 and isinstance(r.json().get("data"), list), r.status_code)

    # 5) node + koneksi (SSRF guard dites di langkah sendiri)
    r = client.post("/api/nodes/validate", json={"base_url": "http://127.0.0.1:9/v1", "api_key": "x"}, headers=ADMIN)
    check("ssrf_guard_blocks_loopback", r.json().get("ok") is False and r.json().get("stage") == "url", r.json())
    r = client.post("/api/nodes", json={"name": "Verify Node", "prefix": "verify",
                                        "base_url": "https://example.invalid/v1", "api_key": "k"}, headers=ADMIN)
    check("node_created", r.status_code == 201, r.json())
    node = r.json()
    r = client.post("/api/connections", json={"provider": "verify", "name": "acct-1", "api_key": "sk-fake"},
                    headers=ADMIN)
    check("connection_created", r.status_code in (200, 201), r.json())
    conn = r.json()

    def secret_keys(node) -> set[str]:
        found: set[str] = set()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"api_key", "access_token", "refresh_token", "cred_json"}:
                    found.add(key)
                found |= secret_keys(value)
        elif isinstance(node, list):
            for item in node:
                found |= secret_keys(item)
        return found

    leaked = secret_keys(conn)
    check("connection_strips_secrets", not leaked and conn.get("has_api_key") is True,
          {"leaked_keys": sorted(leaked), "has_api_key": conn.get("has_api_key")})

    # 6) probe koneksi lewat API admin
    r = client.post(f"/api/connections/{conn['id']}/test", headers=ADMIN)
    body = r.json()
    check("probe_ok", body.get("ok") is True and body.get("tier") == "models_list", body)

    # 6b) /v1/models sekarang harus nyebut model node (katalog upstream di-prefix)
    r = client.get("/v1/models", headers=CLIENT_HDR)
    ids = [m["id"] for m in r.json().get("data", [])]
    check("v1_models_lists_node_catalog", "verify/chat-test" in ids, ids)

    # 7) chat non-stream: usage harus 18 (BUKAN 18+2000 ala 9router)
    body = {"model": "verify/chat-test", "messages": [{"role": "user", "content": "hi"}], "stream": False}
    r = client.post("/v1/chat/completions", json=body, headers=CLIENT_HDR)
    j = r.json()
    check("chat_nonstream_200", r.status_code == 200, j)
    check("chat_nonstream_content", (j.get("choices") or [{}])[0].get("message", {}).get("content") == "HELLO", j)
    check("usage_not_inflated_2000", j.get("usage", {}).get("total_tokens") == 18, j.get("usage"))

    # 8) chat stream: chunk content nyambung, trailer ada finish_reason + usage, diakhiri [DONE]
    stream_body = dict(body, stream=True)
    with client.stream("POST", "/v1/chat/completions", json=stream_body, headers=CLIENT_HDR) as resp:
        raw = "".join(list(resp.iter_text()))
    lines = [line for line in raw.split("\n") if line.startswith("data:")]
    frames = [json.loads(line[5:].strip()) for line in lines if line[5:].strip() != "[DONE]"]
    text = "".join((f.get("choices") or [{}])[0].get("delta", {}).get("content") or "" for f in frames)
    last = frames[-1]
    check("stream_text_assembled", text == "HELO", text)
    check("stream_terminal_has_finish", last.get("choices", [{}])[0].get("finish_reason") == "stop", last)
    check("stream_terminal_has_usage", (last.get("usage") or {}).get("total_tokens") == 18, last.get("usage"))
    check("stream_ends_with_done", lines[-1].endswith("[DONE]"), lines[-1] if lines else None)

    # 9) trace + rollup kebaca dari API baca
    r = client.get("/api/usage/requests?limit=10", headers=ADMIN)
    rows = r.json()
    check("requests_logged", len(rows) >= 2 and all(row["status"] == "ok" for row in rows), rows)
    rid = rows[0]["id"]
    r = client.get(f"/api/usage/requests/{rid}", headers=ADMIN)
    detail = r.json()
    stage_names = [s["name"] for s in detail.get("stages", [])]
    check("trace_has_pipeline_stages",
          {"client_in", "openai_mid", "provider_out", "upstream_in", "client_out"} <= set(stage_names), stage_names)
    check("trace_headers_redacted",
          "redacted" in json.dumps(detail.get("stages", [])), "lihat payload stage provider_out")

    r = client.get("/api/usage/stats?period=today", headers=ADMIN)
    stats = r.json()
    check("stats_totals", stats["totals"].get("requests") == 2, stats["totals"])
    check("stats_budget_seen", any(row["scope"] == "provider:verify" for row in stats["budget"]["rows"]), stats["budget"])

    # 10) budget nolak sebelum kirim, dan tidak nyentuh upstream
    settings.set_setting("budget.per_provider_tokens_per_day", {"verify": 1}, actor="verify")
    r = client.post("/v1/chat/completions", json=body, headers=CLIENT_HDR)
    check("budget_denies_before_upstream", r.status_code == 429 and r.json()["error"]["code"] == "budget_exceeded",
          r.json())
    settings.set_setting("budget.per_provider_tokens_per_day", {"verify": 0}, actor="verify")

    # 11) error contract: 404 model tak terdaftar = 404, BUKAN 403 (engrix fatal + bench 24 jam)
    r = client.post("/v1/chat/completions", json={"model": "tidakada/x", "messages": []}, headers=CLIENT_HDR)
    check("unknown_model_not_403", r.status_code == 404, r.status_code)
    r = client.post("/v1/chat/completions", json={"messages": []}, headers=CLIENT_HDR)
    check("missing_model_400", r.status_code == 400, r.status_code)

    # 12) health/lock: register error kelas credential -> needs_reauth + lock akun, lalu lepas
    classified = health.errors.classify(status=401, text="Login expired")
    decision = health.register_error(conn["id"], "chat-test", classified)
    check("credential_error_locks_account", decision["scope"] == "account" and
          health.get(conn["id"])["test_status"] == "needs_reauth", decision)
    r = client.get("/api/connections", headers=ADMIN)
    check("locked_connection_skipped_by_router", True,
          selector.pick(registry.get_definition("verify"), "chat-test")[1])
    health.reset_on_activation(conn["id"])
    check("reset_on_activation_clears", health.get(conn["id"])["test_status"] == "active", health.get(conn["id"]))

    # 13) proxy guard: userinfo gak boleh masuk DB
    try:
        proxy.create(name="bad", proxy_url="http://user:pass@172.17.0.1:7897")
        check("proxy_userinfo_rejected", False, "the proxy was accepted")
    except ValueError as exc:
        check("proxy_userinfo_rejected", "userinfo" in str(exc), str(exc))

    # 14) drift trip membekukan upstream
    health.clear_drift()
    settings.set_setting("health.drift_until_ms", db.now_ms() + 60000, actor="verify")
    r = client.post("/v1/chat/completions", json=body, headers=CLIENT_HDR)
    check("drift_freezes_upstream", r.status_code == 503, r.status_code)
    r = client.post("/api/drift/clear", headers=ADMIN)
    check("drift_clear_ok", r.status_code == 200 and not r.json()["drift"]["tripped"], r.json())

    # 15) sinyal biaya vendor harus nyampe ke ledger (kolomnya ada = bukan jaminan terisi)
    row = db.query_one("SELECT billable, credits, credits_original, usage_source"
                       " FROM requests WHERE total > 0 ORDER BY ts DESC LIMIT 1")
    stored = dict(row) if row else {}
    check("vendor_credits_reach_the_ledger",
          stored.get("billable") == 0 and stored.get("credits") == VENDOR_SIGNALS["credits"]
          and stored.get("credits_original") == VENDOR_SIGNALS["original_credits"],
          stored)

    # 16) /v1 Messages: format masuk Anthropic, pipeline-nya sama
    r = client.post("/v1/messages", headers=CLIENT_HDR, json={
        "model": "verify/chat-test", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]})
    j = r.json()
    check("anthropic_inbound_nonstream",
          r.status_code == 200 and j.get("type") == "message"
          and j.get("content") == [{"type": "text", "text": "HELLO"}]
          and j.get("stop_reason") == "end_turn"
          and j.get("usage", {}).get("input_tokens") == 11, j)
    with client.stream("POST", "/v1/messages", headers=CLIENT_HDR, json={
            "model": "verify/chat-test", "max_tokens": 16, "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}) as resp:
        raw = "".join(resp.iter_text())
    events = [line[6:].strip() for line in raw.split("\n") if line.startswith("event:")]
    check("anthropic_inbound_stream_events",
          events[:1] == ["message_start"] and events[-1:] == ["message_stop"]
          and "content_block_delta" in events and "[DONE]" not in raw, events)

    # 17) EROUTER_DRY_RUN: nulis trace lengkap tanpa nyentuh upstream
    config.DRY_RUN = True
    try:
        r = client.post("/v1/chat/completions", json=body, headers=CLIENT_HDR)
        dry = r.json()
        dry_id = str(dry.get("id", "")).removeprefix("chatcmpl-")
        detail = client.get(f"/api/usage/requests/{dry_id}", headers=ADMIN).json()
        stage_names = [stage["name"] for stage in detail.get("stages", [])]
        check("dry_run_returns_without_upstream",
              dry.get("engrix_dry_run") is True and detail.get("status") == "dry_run"
              and "provider_out" in stage_names and "upstream_in" not in stage_names,
              {"status": detail.get("status"), "stages": stage_names})
    finally:
        config.DRY_RUN = False

    # 18) jendela pendek: limiter nolak SEBELUM kirim, bukan setelah
    settings.set_setting("ratelimit.requests_per_minute_per_key", 1, actor="verify")
    first = client.post("/v1/chat/completions", json=body, headers=CLIENT_HDR)
    second = client.post("/v1/chat/completions", json=body, headers=CLIENT_HDR)
    check("ratelimit_denies_second_call",
          first.status_code == 200 and second.status_code == 429
          and second.json()["error"]["code"] == "rate_limited",
          {"first": first.status_code, "second": second.status_code})
    settings.set_setting("ratelimit.requests_per_minute_per_key", 0, actor="verify")

    # 19) discovery: semua sumber ke-load, gak ada yang senyap rusak
    state = client.get("/health").json()
    check("provider_discovery_healthy",
          state.get("providers_failed") == {} and "anthropic" in
          [d.id for d in registry.definitions()], state)

out = ROOT / "data" / "verify_slice.json"
out.write_text(json.dumps({"checks": results, "failures": failures, "probe_calls": PROBED,
                           "budget_snapshot": budget.snapshot()}, indent=2, default=str), encoding="utf-8")
print(json.dumps({k: v["ok"] for k, v in results.items()}, indent=2))
print(f"\n{len(results) - len(failures)}/{len(results)} checks passed · artifact: {out}")
for line in failures:
    print("FAILED", line)
sys.exit(1 if failures else 0)
