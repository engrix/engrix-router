# Architecture

Local OpenAI/Anthropic-compatible gateway, ported from a reference implementation
(9router v0.5.95) whose defects this build deliberately does not reproduce. Every claim here is
traceable to a file in this tree; where the provenance is the reference implementation, the path
is marked `9router:` and points at that upstream project, not at a local checkout.

Read the ADRs first: [0000 philosophy and boundaries](adr/0000-philosophy-and-boundaries.md),
[0001 canonical format](adr/0001-canonical-format.md),
[0002 public core / private providers](adr/0002-public-core-private-providers.md),
[0003 layer map](adr/0003-layer-map.md).

## 1. One request, the data path

```
client (OpenAI- or Anthropic-shaped)
  POST /v1/chat/completions              src/engrix_router/api/openai.py
  POST /v1/messages                      src/engrix_router/api/anthropic.py
                                         (Anthropic body -> canonical via
                                          core/formats/anthropic.py: to_canonical_body;
                                          a bare model id gets the `anthropic/` prefix)
  -> deps.require_client_key             src/engrix_router/api/deps.py     (SHA-256 hash lookup)
  -> runner.run_chat | stream_chat | stream_frames   src/engrix_router/pipeline/runner.py
      1 _resolve     prefix/model -> ProviderDef     providers/registry.py (no guessing: 404)
        stage(1 client_in) written on the raw body     subscribers/trace.py
      2 _admission   drift kill-switch, rate limit window, daily budget
                                                     accounts/health.py, hooks/ratelimit.py,
                                                     hooks/budget.py
      3 stage(2 openai_mid, 3 provider_out)          subscribers/trace.py
      4 selector.pick -> connection candidates      routing/selector.py
                                                     (accounts/connections.py lists them)
      5 provider.build_request -> open_stream / complete   providers/base.py
      6 frames -> ONE canonical frame stream, rendered per wire format:
         OpenAI SSE + [DONE]              core/formats/openai.py  (api/openai.py renders)
         Anthropic events                 core/formats/anthropic.py: StreamRenderer
                                          (api/anthropic.py renders, no [DONE] sentinel)
      7 stage(4 upstream_in, 5 client_out) -> usage merge -> trace.finish -> daily rollup
                                                     subscribers/usage.py, subscribers/trace.py,
                                                     subscribers/pricing.py
      (dry run only) stage(6 dry_run) + status `dry_run`, budget settled to 0
  -> read back: /api/usage/stats | /api/usage/requests/{id} | /api/logs/stream
```

`runner.stream_frames(body, ..., render=..., request_id=...)` is the seam that makes
[ADR-0001](adr/0001-canonical-format.md) "one pipeline, N wire formats" real: it yields canonical
frames, and each inbound format renders them. `render` is passed in only so the trace stores the
exact bytes the client saw instead of a re-serialization that could drift from what was sent.
Non-streaming answers are assembled from the same frames, so both paths share one recording
pipeline. A stream failure is delivered as an error frame plus `[DONE]` (OpenAI) or an `error`
event (Anthropic) with no fabricated `finish_reason`, because after the first byte the HTTP status
can no longer change.

## 2. Layers as implemented

The authoritative owner of this section is [ADR-0003](adr/0003-layer-map.md): the old-package ->
new-layer mapping table, the five recorded deviations from the sketch, and who owns each contested
decision all live there and are not duplicated here.

The ranks below are the ones `tests/test_layering.py` enforces (AST-based, no external
dependency, so the rule holds on a fresh clone):

```
core(0) < storage(1) < transport(2) = identity(2) = auth(2)
        < providers(3) = accounts(3)
        < routing(4) = hooks(4) = subscribers(4) = services(4)
        < pipeline(5) < api(6) = testing(6) < web(7)
```

Two invariants, both tested: a module may import only its own layer or below, and siblings inside
one band may not import each other -- `{providers, accounts}` and
`{routing, hooks, subscribers, services}`. `testing` sits at rank 6 with `api` because the shared
contract suite deliberately drives the full pipeline; nothing below it may reach up into it.
`import-linter` is the `[dev]` belt used by CI; the test is the part that cannot be skipped.

## 2b. Porting map: reference implementation -> this tree

| concept | provenance | here |
|---|---|---|
| DB schema | 9router: live `_vps_runtime/data_schema.sql` dump | `src/engrix_router/schema.sql` (only DDL for new databases; deviations D1-D4 commented in place) + `storage/sqlite.py` `SCHEMA_VERSION` / `MIGRATIONS` for existing ones |
| `prefix/model` resolution | 9router: `open-sse/services/model.js:13-46` | `providers/registry.py: split_model / resolve` (unknown prefix = 404, never a fallback) |
| provider definition | 9router: `providers/registry/*.js`, `schema.js:8-46` | `core/types.py: ProviderDef / TransportSpec / AuthSpec` + `DEFINITIONS` inside the vendor file |
| default executor | 9router: `open-sse/executors/default.js:69-351` | `providers/openai.py` |
| executor hooks | 9router: `open-sse/executors/base.js:11-189` | `providers/base.py` (build and send are separate steps) |
| selection strategy | 9router: `open-sse/services/auth.js:139-196` | `routing/selector.py` (+ `accounts/connections.py: candidates`) |
| lock + cooldown | 9router: `accountFallback.js:121-165`, `config/errorConfig.js:59-86` | `accounts/health.py` + `core/errors.py` |
| SSE unwrap and coalescing | 9router: vendor executor + its `sse.js:105-208` | `core/formats/openai.py` (finish + usage trailer) + `BaseProvider.unwrap_data` per vendor |
| canonical usage | 9router: `utils/usageTracking.js:164-335` | `subscribers/usage.py` (+ `vendor_signals` / `merge_signals`) |
| pricing | 9router: `providers/pricing.js:410-491` | `subscribers/pricing.py` |
| proxy pool | 9router: `lib/network/connectionProxy.js`, `proxyPoolsRepo.js` | `transport/proxy.py` |
| catalog cache | 9router: `services/<vendor>Models.js:38-39,204-279` | `accounts/catalog.py` (in-RAM TTL, in-flight join) + the `provider_catalog` mirror table a vendor adapter may fill per account |
| per-connection quota | 9router: `api/usage/[connectionId]/route.js:133-196` | `accounts/limits.py` + `quota_snapshots` table + `services/quota_sync.py` |
| vendor with its own protocol | 9router: one executor per vendor plus its shared helpers | **the private adapter distribution** (ADR-0002): a subscription adapter is not in this repository. It attaches through the `engrix_router.providers` entry-point group or `EROUTER_PROVIDERS_PATH` and runs the same contract suite |
| log format and ring | 9router: `chatCore.js:251`, `requestDetail.js:85-101`, `lib/consoleLogBuffer.js` | `core/logs.py` |
| trace ring and retention | 9router: `requestDetailsRepo.js:80-86,127-133` | `subscribers/trace.py` |
| console log SSE | 9router: `api/translator/console-logs/stream` | `api/admin_observability.py: logs_stream` |
| per-request stage dump | 9router: `api/translator/{load,send,translate}`, `utils/requestLogger.js:17` | `subscribers/trace.py` stage rows |
| client API keys | 9router: `apiKeysRepo.js:28-75`, `apiKey.js:3-34` | `identity/api_keys.py` (changed, see D2) |
| OpenAI-compatible node | 9router: `providerNodes` + `api/provider-nodes/*` | `nodes` table, `api/admin_nodes.py`, `providers/openai.py: build_node_definition` |
| Anthropic Messages format | no counterpart in the reference | `core/formats/anthropic.py` (inbound + outbound, inverse pair), `api/anthropic.py`, `providers/anthropic.py` |
| short-window rate limiting | reference had a daily budget only | `hooks/ratelimit.py` |
| provider discovery | 9router: hand-written `providers/registry/index.js` labelled "auto-generated" | `providers/registry.py`: three scanned sources, per-source failure isolation, `DuplicatePrefix` on collision |

## 3. Structural deviations, and the reason for each

* **D1: health and locks are separate tables, not part of the credential blob.** The reference
  stored `testStatus/errorCode/backoffLevel/lastUsedAt/consecutiveUseCount/modelLock_<model>` in
  the same JSON as the access token, so every lock write rewrote the token and "who else is
  locked" was not a query. Here: `connection_health` and `model_locks`, 1:1 with `connections`.
* **D2: client keys are hashed.** The reference kept `key TEXT UNIQUE` in plaintext, looked keys up
  by value, and returned every intact key from its list endpoint; its generator mixed
  `Math.random()` with a hardcoded default HMAC secret. Here: `secrets.token_urlsafe(32)`, stored
  as SHA-256 plus a 12-character display prefix, returned exactly once at creation.
* **D3: failures are recorded.** The reference wrote usage history only on success (in the owner's
  database: 35,453 rows, zero with a non-ok status), so a quota-draining failure was invisible.
  Here: one `requests` row per request, success or failure, and `status` + `error_class` +
  `http_out` + `upstream_status` on it.
* **D4: one `request_id` (time-ordered id) is the join key** across `requests`,
  `request_stages` and log lines. The reference had no correlation at all: random stream ids and
  log tags derived from a session hash.
* **The retry ladder lives in the runner, not in the provider.** In the reference a vendor executor
  could override the whole execute path and silently lose the ladder -- that vendor ran with zero
  retries and zero URL fallbacks for several versions. Here a provider owns exactly one attempt
  (`pipeline/runner.py: _dispatch`), under an attempt cap
  (`routing.max_connections_per_request`) and a global deadline.
* **A provider is a singleton, so no per-request state may live on `self`.**
  `registry.get_provider(prefix)` caches one instance per provider id, shared by every concurrent
  request. That is why `transform_request()` receives `creds` and why the runner calls
  `build_request()` again for each candidate: an early version stored the connection id on the
  instance and would have overwritten another account's catalog and session material as soon as
  round-robin touched a second connection.
* **Replay is re-signed per attempt.** Because `build_request()` is pure and re-run per candidate,
  request id, timestamp and signature are always fresh. Replay-sensitive upstreams punish an
  identical signature (the reference logged `403 code 103 Duplicate request`; our own probe
  measured a silent hang). The requirement is documented in `providers/base.py`.
* **The registry scans; nobody hand-maintains an import list.** A missed import in the reference
  meant a provider quietly vanished from the UI and from model parsing, and an unmapped connection
  fell through to the generic OpenAI executor -- i.e. traffic to the wrong vendor. Here three
  sources are scanned (built-in modules and packages, entry points, `EROUTER_PROVIDERS_PATH`), a
  broken source is isolated and reported (`registry.load_errors()`, surfaced by `/health` as
  `providers_failed`, logged at startup), and a duplicate prefix is `DuplicatePrefix` at startup.
* **No silent provider fallback.** A model without a prefix, or with an unknown one, is a 404.
* **No `+2000` token buffer.** The reference added 2,000 tokens to prompt and total in every
  client-visible `usage` block, which inflated the caller's ledger by 2,000 per call. Here usage
  reaching the client is the upstream number; when the upstream sends none, an estimate is used and
  flagged through `usage_source`. Real headroom is a budget concern
  (`budget.reserve_headroom_tokens`), never a reported number.
* **Settings and credentials are not one blob.** The reference stored settings as a single-row blob
  (polluted with hundreds of junk numeric keys in the owner's database) and kept dashboard
  passwords and client keys inside it. Here: one key per `settings` row, validated against the
  `DEFAULTS` registry, no secret ever travels through settings, and proxy credentials come from env
  through `credential_hint`. Vendor knobs are registered by the vendor package itself
  (`settings.register_defaults(..., owner="<vendor>")`), so adding a provider never edits the core
  defaults.
* **Cost signals reach the ledger.** `subscribers/usage.py: vendor_signals()` pulls `billable`,
  `credits` and `original_credits` out of any vendor usage block, `merge_signals()` keeps the last
  value per key, `RequestTrace.attach_usage(..., signals=...)` stores them and `trace.finish()`
  writes `requests.billable / credits / credits_original`. Credits stay in the vendor's own unit and
  are never converted to USD there; USD comes from `subscribers/pricing.py`. That is why
  `SCHEMA_VERSION` is 2 and why `MIGRATIONS` exists: a DDL change is a version bump plus one
  migration entry, `tests/test_schema_migrations.py` proves an old database is upgraded idempotently
  and that a build refuses a database newer than itself.

## 4. Status contract to the client (the part that matters most)

The consuming client treats `400/401/403` as fatal and benches a gateway API key for 24 hours. So
the rule is: **`401/403` are reserved for "you are not authenticated to this gateway"**; every
upstream-side problem surfaces as `503`/`429` with `Retry-After`. One policy table decides this, in
`core/errors.py`; providers only raise `UpstreamError(status=..., vendor_code=...)` and never choose
a client status, and the class name is what the client's SDK branches on (`error.code`).

| upstream condition | class | to the client | state effect |
|---|---|---|---|
| vendor code 110 (daily quota spent) — and ZCode's code **1005**, which the same vendor sends as an **HTTP 200** body | `quota_daily` | `503` + `Retry-After` until the next UTC midnight | account lock until the reset; the request **fails over to the next connection** (a dry account must not black out the provider) |
| vendor code 112 / pricing URL in the message | `pricing_blocked` | `503`, `Retry-After` 3600 | model lock, 1 hour |
| vendor code 10605 (queue) | `queue_throttled` | `429` + `Retry-After` 8 | short lock; may retry internally |
| vendor code 3012 (ZCode edge rejects the request **shape**) | `anti_abuse_shape` | `503` + `Retry-After` 120 | model lock only (`health.anti_abuse_cooldown_ms`); the account stays usable and there is no calendar window to wait for |
| `401/403` **from upstream** (our credential is dead) | `credential_dead` | `503` + `Retry-After` 120 | connection -> `needs_reauth` |
| bad signature / replay (`code 103`) | `protocol_drift` | `502` | **global freeze** 15 minutes after `health.protocol_drift_threshold` hits; cleared by `POST /api/drift/clear` |
| `429` / rate-limit text | `rate_limited` | `429` + backoff | exponential backoff (`health.backoff_*`) |
| `5xx`, timeout, stalled stream | `upstream_unavailable` / `upstream_timeout` / `stream_stalled` | `503` / `504` | transient cooldown; internal retry only before the first byte |
| our own daily budget is exceeded | `budget_exceeded` | `429` + `Retry-After` 3600 | nothing sent upstream |
| no healthy connection left | `all_locked` | `503` | nothing sent |
| upstream `400/406` | `client_bad_request` / `model_not_found` | `400` / `404` | **never locks an account** by default (`health.lock_plain_4xx = false`) |
| no / wrong gateway key | `client_no_key` / `client_bad_key` | `401` | the only place 401 means "auth" |

Mid-stream failure: an error frame then `[DONE]` (OpenAI) or an `error` event (Anthropic), never a
fake `finish_reason`. The reference made the same choice for the same reason, and openai-python
raises when it sees an `error` key, which the client checks before `[DONE]`.

## 5. Timeouts (a two-sided contract)

| | gateway | reference | client |
|---|---|---|---|
| connect | 20 s | 60 s (120 s for one vendor) | -- |
| first token | **25 s** | no first-token timeout on that path (200 s used) | round deadline **30 s** |
| stall between chunks | 120 s | 360 s (120 s for one vendor) | gap handling, scaled |
| total | 300 s | -- | 600 s |

Env names: `EROUTER_UPSTREAM_CONNECT_TIMEOUT_S`, `EROUTER_UPSTREAM_FIRST_CHUNK_TIMEOUT_S`,
`EROUTER_UPSTREAM_STALL_TIMEOUT_S`, `EROUTER_UPSTREAM_TOTAL_TIMEOUT_S`
(`core/config.py`; the client-side numbers come from the consuming agent repository, not from this
tree). The point is functional, not aesthetic: the gateway must cut a call off **before** the
client's own 30 s round deadline, otherwise the client abandons and retries while the vendor is
still thinking, which is double upstream spend for one answer.

## 6. Security posture

Bind `127.0.0.1` by default. `/api/*` fail-closed when `EROUTER_ADMIN_TOKEN` is empty (`503`, never
a hardcoded default) and startup logs a warning. Client gate on `/v1/*` by default, and loopback
does not pass automatically. SSRF guard on operator-supplied URLs
(`transport/url_guard.py`): loopback, link-local/metadata, private, reserved, multicast and
unspecified addresses plus `.localhost`/`.internal`/`.local` hostnames, with
`nodes.allow_private_urls` as the deliberate escape hatch. Proxy URLs may not carry userinfo --
credentials come from env via `credential_hint`. Credential headers are redacted on every log and
trace path (`core/logs.py: redact_headers`), and stage payloads are truncated with explicit
`truncated` + `original_bytes` markers instead of a silent substring. Conversation payloads stay in
the local database and can be turned off (`observability.enabled=false`). Details and the private
reporting channel: [SECURITY.md](../SECURITY.md).

## 7. Not implemented, and why

Each of these was considered and refused for a measured reason -- they are not backlog omissions:

* **Token refresh** -- no provider in this codebase has a verified refresh call. The device-token
  refresh of the subscription vendor was rejected by that vendor in our probes, and the
  PAT-style exchange has a documented route but an undocumented body, and PAT-style flows live in
  adapters, not here. Writing a refresher now would mean inventing a request shape and calling it a
  feature. Reason recorded in `services/quota_sync.py`.
* **A periodic health prober** -- the connection breaker already learns health from real traffic, and
  a probe that spends credits duplicates what the next real request would have told the breaker, at
  a cost (measured in the port: 404-shaped requests drained trial credits in under a day). The
  zero-credit alternative exists and is reachable: `provider.probe()` via
  `POST /api/connections/{id}/test`.
* **Response caching, guardrails, prompt packs, tool execution, MCP, embeddings, the Responses API**
  -- rejected by [ADR-0000](adr/0000-philosophy-and-boundaries.md). Two REST surfaces only.

Still inherited, and fair game for the next change: `connections.cred_json` is plaintext in the
local SQLite file (a keyring or a separate 0600 secret file is the candidate); the per-account
enable/price flags are not surfaced in the dashboard; `TransportSpec` carries no
`requires_stream`/`validate_url` (fields that nothing read were removed instead of left as
decoration); runtime `nodes` are served only by
`OpenAICompatibleProvider` regardless of the recorded `type`.

**Placeholders that do not exist yet:** `routing/topology/`, `ml/` and `docs/providers/` are not in
this repository and are **not designed yet**. Their names appear in the ADRs as out-of-scope
reservations, not as architecture. Do not read a directory listing into them.
