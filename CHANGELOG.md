# Changelog

All notable changes to `engrix-router` are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the version
numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`0.1.0` is both the unreleased section and the first release: it describes the state of the
tree as it was cut. There is no git history before it yet, so there are no per-change commits
to list — the entries below are grouped by what the code actually does, and `Fixed` names the
defects of the reference implementation this gateway was ported from (9router v0.5.95) that
this build does not reproduce.

## [Unreleased]

### Added

- OAuth+ account linking: `/api/oauth/{provider}/start` + `/api/oauth/{provider}/{session}`
  run the vendor's own browser login flow (provider packages contribute `oauth_start` /
  `oauth_poll` hooks; core owns the session state machine, single-use expiry, local
  poll throttling and the one-row `connections.create` persistence). The dashboard's
  Connections tab shows an OAuth+ button for every provider that supports it and paints
  the resulting account without the operator ever pasting a token. 9router's counterpart
  is manual token pasting; the link path here never sees a raw vendor token in a form.
- Reasoning accounting: a streamed `reasoning_content` delta with no `reasoning_tokens`
  in the vendor usage block is estimated (chars/4) into `requests.reasoning` instead of
  staying zero; TTFT is now stamped on the first frame carrying visible text or
  thinking, not on an empty role/ping frame (measures first seen token, not handshake).
- ZCode vendor classification: edge anti-abuse code 3012 ("unusual activity") has its own
  `anti_abuse_shape` class — `503` + `Retry-After` 120 with a model-scoped lock
  (`health.anti_abuse_cooldown_ms`), no internal retry, no calendar window. TASK-48 measured
  the trigger to be where the vendor's own scaffold sits in the body, not a daily quota.
- Registry: the same provider module discovered through two paths (installed entry
  point + `EROUTER_PROVIDERS_PATH` under an editable install) no longer raises
  `DuplicatePrefix` — only genuinely different modules claiming one prefix do.

### Added

- Dashboard is a React SPA now (Vite project under `web/ui`, built into `static/ui`,
  served by `dashboard.py` which only injects the locale catalogs): grouped sidebar
  navigation by intent, a Providers hub that merges accounts + health + per-connection
  quota bars + the OAuth+ button into one card per vendor, countdowns next to every
  quota row, and every operator-visible number passes a formatter — no raw epochs,
  no unlabelled fields, no `NaN` on screen (TASK-43; the Jinja template and its
  innerHTML painters were deleted with this).
- Dashboard: sidebar layout with the official brand lockup, standalone sign-in gate,
  animated brand-pack mascots (gate, error card, every empty table state), card header
  bands with dividers, and a "Client quick start" card that prints the live base URL.
- Brand: `engrix router` combined pixel lockup generated with the brand pack's own
  wordmark geometry; three new glyphs (`o`, `u`, `t`) authored in the same grid language.
- i18n: settings docs are English in the registry and translated in the ID locale via
  `settings.doc.<key>` overlay keys; locale switch repaints the whole page including the
  status line.
- Webfonts: Inter + JetBrains Mono loaded the same way engrix-agent/web does; before this
  the dashboard silently fell back to Segoe UI/Consolas.
- README: lockup header (dark/light), CI/license/Python badges, dashboard screenshots
  taken from a scratch database with fabricated data.
- `/static` and the dashboard page now answer `Cache-Control: no-cache` (etag revalidation
  keeps it cheap); browser heuristic caching had begun serving stale CSS.
- `docs/ATTRIBUTION.md`: 9router v0.5.95 is MIT (verified from its own LICENSE); this tree
  reimplements behavior and copies no source, and the notice ships regardless.

### Changed

- README, `.env.example`, CONTRIBUTING and this changelog no longer name the private provider
  distribution or frame adapters as reverse-engineered; the landing surface says "pluggable
  providers" and [ADR-0002](docs/adr/0002-public-core-private-providers.md) keeps the full
  record. Vendor-specific example filenames were replaced with generic ones.
- The `EROUTER_` env prefix is documented as public API and fixed (a decision, not drift).

### Fixed

- **A single ZCode edge 1005 no longer benches an account that still has quota.**
  The edge can emit `1005 "exceed quota limit"` for an account whose own
  entitlement endpoint still reports plenty left (measured TASK-57: fresh
  account, 100% remaining on both scopes, account-wide `*` lock 8106 s one
  second after its first request). Two guards now share one rule — the vendor's
  quota snapshot wins over a one-shot edge error: `register_error` demotes the
  account lock to a short per-model cooldown when the meter says there is room,
  and a fresh snapshot (fetched at/after the lock) releases an already-installed
  false-positive lock and cools `unavailable` back down to `cooling` via
  `reconcile_quota_locks` (wired into quota snapshot storage, floor
  `health.quota_reconcile_min_pct`, default 5%). Self-correcting: if the edge
  really meant it, the next request 1005s again and re-locks for real.

- **The per-model probe no longer looks like a probe, and can no longer bench a
  healthy account.** `POST /api/connections/{id}/test_models` used to send
  `max_tokens: 8` + `"reply with OK"` — the exact micro shape ZCode's edge blocks
  (405/3012). Each rejection locked one `(account, model)` pair for 120 s, so testing
  three new accounts walked the whole provider into `all_locked`, and because the
  vendor flags the *client*, real agent traffic that had returned 200 for an hour
  started failing on the same body shape. The probe body is now a provider hook
  (`BaseProvider.probe_body`; default = a natural sentence with `max_tokens: 64`), and
  a probe's failure reports its own verdict without touching fleet health.
- **A locked provider says how long it stays locked.** When every candidate was
  locked the answer carried our generic 15 s default while the locks still had ~100 s
  to run, so a client with a retry ladder hammered it (measured: 9 retries in 31 s).
  `selector.next_opening_ms()` (9router `auth.js:114-133`) now reports the earliest
  expiry as `Retry-After`, and the OpenAI error body carries `retry_after`/`reset_at`
  instead of hand-building a body that dropped them.
- **`tool_choice` strings are translated, not forwarded.** The canonical -> Anthropic
  translator passed OpenAI's `"auto"`/`"none"` through verbatim, so every Anthropic-shaped
  vendor answered `400 body.tool_choice: Input should be a valid dictionary`, and a client
  that sent the proper Anthropic object hit `TypeError: unhashable type: dict` in
  `choice in {"auto","none"}`. Strings now become `{"type": ...}`, `"required"` stays
  `{"type":"any"}`, an Anthropic object passes through untouched, and an absent value
  emits no key. (Found on live engrix-agent traffic; the agent was sending valid OpenAI.)
- **A spent daily quota is no longer served as an empty success.** ZCode's start-plan route
  answers `{"code":1005,"msg":"exceed quota limit"}` with **HTTP 200** (not 429/503, no SSE).
  It was forwarded to the client as a completion chunk with no content, booked in the ledger
  as a healthy call and left the connection marked `active`. The adapter now rejects the
  envelope, `classify` knows code 1005 as `quota_daily`, and `quota_daily` may fail over to
  the next connection — a dry account no longer blackouts the provider while a funded one
  sits behind it.
- **One rejected body no longer kills the provider for the rest of the day.** Code 3012 used
  to classify as `quota_daily`, which took `unavailable` status plus a `'*'` account lock until
  the next UTC midnight: two accounts answering a shape question wrongly were both removed from
  routing for ~6 hours and every later request short-circuited to `503 all_locked`. It is now
  `anti_abuse_shape` (model-scoped, `health.anti_abuse_cooldown_ms`, default 120 s).
- The quota banner's `worst_remaining()` filtered on the global `MAX(fetched_at)`:
  with several connections polled at different moments it only ever considered the
  last one and could hide a 0%-left row from an earlier one. It now takes each
  connection's own newest snapshot.
- Connection countdowns printed `NaNd NaNh NaNm`: the backend stores `expires_at` as
  an ISO string while the formatter expected epoch-ms — `dcount()` normalizes both,
  and remaining-percentage is clamped to 0..100 so vendor float rounding can no
  longer render "-0% left".
- The log tab showed a bare `aborted` badge before anything had run: opening the tab
  now starts the 60 s tail automatically, and the badge distinguishes stopped /
  running / finished / cancelled instead of collapsing everything into `aborted`.
- Settings registry doc strings were partly Indonesian in an otherwise-English surface;
  they are English-only now.
- A leaked proxy credential pair and the owner's VPS hostname in source docstrings
  (removed during the security audit; the same audit scrubbed every private-report
  pointer from the public tree).

## [0.1.0] - 2026-10-09

### Added

**Request pipeline** (`pipeline/runner.py`)
- Multi-connection failover: one request iterates candidate connections from
  `routing/selector.py` under an attempt cap (`routing.max_connections_per_request`) and a
  global deadline, with a short inter-attempt pause that never sleeps for the vendor's own
  Retry-After.
- The retry ladder lives in the runner, not in the provider: a provider owns exactly one
  attempt, so a vendor-specific executor cannot silently drop retries.
- Failover only before the first byte reaches the client; after that a mid-stream failure is
  delivered as an error frame plus `[DONE]`, with no fabricated `finish_reason`.
- Admission control before sending: the drift kill-switch and the daily budget are checked
  first, so a refused request costs nothing upstream.
- `EROUTER_DRY_RUN`: the whole pipeline runs (resolve, admission, routing, `build_request`,
  trace) and the upstream is never contacted; the reply is a synthetic `stop` chunk carrying
  `engrix_dry_run: true`, for diffing payloads without spending credits.

**Budget, health, identity, transport**
- Budget reserve/settle (`hooks/budget.py`): per-UTC-day buckets for global, per-provider,
  per-lane and per-key scopes; reserve uses prompt estimate plus `budget.reserve_headroom_tokens`
  and settle corrects the bucket against real upstream usage (the delta may be negative).
- Connection health (`accounts/health.py`): auth/transient/short cooldowns, rate-limit
  backoff, model locks, `needs_reauth` on a dead credential, reset-on-activation, and a
  protocol-drift kill-switch that freezes *every* upstream after
  `health.protocol_drift_threshold` signature/replay hits and clears on
  `POST /api/drift/clear`.
- Client API keys (`identity/api_keys.py`): `secrets.token_urlsafe(32)`, stored as SHA-256
  hash plus display prefix, emitted exactly once at creation; request rows reference
  `api_key_id` only.
- SSRF guard (`transport/url_guard.py`) for operator-supplied node `base_url` values:
  loopback, link-local/metadata, private and reserved addresses plus internal hostnames, with
  `nodes.allow_private_urls` as the explicit escape hatch.
- Proxy pool (`transport/proxy.py`): credential-free stored URLs, credentials resolved from
  env through `credential_hint`, userinfo rejected, and an optional strict mode that fails
  rather than silently going direct.
- Shared httpx client pool keyed by `(proxy, event loop)`, with closed loops dropped.

**Inbound formats**
- OpenAI surface: `POST /v1/chat/completions` (stream and non-stream) and `GET /v1/models`.
- Anthropic surface: `POST /v1/messages`, rendered from the same canonical frame stream, with
  Anthropic-typed events translated per event and `tool_use` blocks mapped onto OpenAI
  `tool_calls` without any per-stream state.

**Providers**
- `providers/openai.py`: the generic OpenAI-compatible upstream (bearer or header
  auth from `TransportSpec`, model catalogue from the upstream's own `/models`, default probe
  `GET /models` costing zero tokens).
- `providers/anthropic.py`: Anthropic Messages upstream (`x-api-key` + `anthropic-version`,
  mandatory `max_tokens` filled from a documented default, `system` hoisted out of messages).
- Runtime providers from the `nodes` table: an operator-defined prefix becomes a routable
  provider with no code written, invalidated through `providers/registry.py` when nodes change.

**Provider discovery** (`providers/registry.py`)
- Three scanned sources, no hand-edited import list: built-in modules in `providers/`,
  installed entry points in the `engrix_router.providers` group, and `EROUTER_PROVIDERS_PATH`
  for adapters run straight from a checkout.
- Per-provider load isolation: a broken adapter is recorded, excluded from routing and
  reported in `/health` (`providers_failed`) and `/api/state` instead of taking the working
  ones down or vanishing silently.
- `DuplicatePrefix`: one model prefix claimed by two sources is a hard startup error, not
  "first one wins".
- An unknown provider or a model without a prefix is a 404, never a quiet fallback.

**Storage**
- SQLite (WAL) schema at version 2 (`src/engrix_router/schema.sql`) with a
  `SCHEMA_VERSION` + `MIGRATIONS` bootstrap in `storage/sqlite.py` that upgrades an existing
  database idempotently and refuses to downgrade; checksum mismatch at the same version is an
  error rather than a silent re-stamp.
- v2 migration adds `requests.credits` and `requests.credits_original` (vendor credit units,
  not USD), and `requests.billable` is now written from the vendor's usage signals through the
  trace's `finish()`, alongside cost, tier and per-scope usage rollups.
- Daily usage rollup in the same transaction as the request row update; ring cap plus
  retention trimming on `requests`/`request_stages`.
- Runtime settings (`storage/settings.py`): one row per key, typed validation against the
  `DEFAULTS` registry, explicit cache invalidation on write, and `register_defaults()` so a
  provider package declares its own keys without editing core.

**Observability and dashboard**
- Per-request trace with six named stages — `client_in`, `openai_mid`, `provider_out`,
  `upstream_in`, `client_out`, `dry_run` — one row per request for successes *and* failures,
  payloads truncated with explicit `truncated`/`original_bytes` markers, headers redacted.
- Read API: `/api/usage/stats`, `/api/usage/chart`, `/api/usage/requests`,
  `/api/usage/requests/{id}`, `/api/quota`, `/api/quota/history`, `/api/logs`,
  `/api/logs/stream`, `/api/state`, plus `/health` (public, liveness only).
- Admin dashboard (Engrix-themed, FastAPI templates at `/`): overview, requests and trace
  drill-down, connections, nodes, proxy pools, budgets, settings, quota and logs; it asks for
  the admin token once and keeps it in `localStorage`.
- OpenAPI docs at `/api/docs`.

**Docs and tooling**
- `docs/ARCHITECTURE.md` plus ADRs: `0000` philosophy and boundaries, `0001` canonical format,
  `0002` public core / private providers, `0003` layer map, `0004` file naming.
- `docs/ADDING_A_PROVIDER.md`, `scripts/bootstrap_db.py`, `scripts/verify_slice.py` (39 checks,
  no outbound network).
- Test suite (100 tests, offline) covering contracts, layering, schema migrations, registry
  discovery including provider *packages*, the language policy for docstrings/strings AND for
  `#` comments, hygiene, and the private-material guards.
- Public repository hygiene for this release: `LICENSE` (Apache-2.0), `SECURITY.md`,
  `CONTRIBUTING.md`, this changelog, `.env.example`, `config.example.yaml`,
  `.pre-commit-config.yaml`, CI workflow, and `deploy/` container files.

### Changed

- Timeouts are gateway-owned and tighter than the ported reference: 20 s connect, 25 s
  first chunk, 120 s inter-chunk stall, 300 s total, so the gateway aborts before the client's
  own 30 s round deadline instead of burning a second upstream round.
- Extension points are data, not branches: providers, hooks, subscribers and storage backends
  are discovered by a registry, and `if provider == "..."` outside `providers/` is a rejection.
- Health and lock state live in `connection_health` / `model_locks` instead of inside the
  credential blob, so "who else is locked" is a query and a lock write never rewrites a token.
- Configuration split is explicit: env (`core/config.py`) for anything needing a restart,
  `settings` table for anything an operator changes live — never both for one value.
- Language policy is enforced, not intended: English for every operator- and client-facing
  surface, Indonesian only inside `#` comments. Both halves now have teeth --
  `tests/test_language_policy.py` reads the AST, `tests/test_comment_language.py` reads the
  tokenizer and judges comment *blocks* by English function words, so a tech term inside an
  Indonesian comment (stream, chunk, payload) is not a violation.
- Module names are symmetric per ADR-0004: `core/formats/openai_sse.py` -> `openai.py`,
  `core/formats/anthropic_messages.py` -> `anthropic.py`, `api/compat.py` -> `api/openai.py`,
  `providers/openai_compatible.py` -> `openai.py`. A vendor now has one token, used in every
  layer, so a reader can guess a path instead of memorising four naming schemes. The old names
  are stale-reference patterns in CI.
- `TransportSpec.requires_stream` and `validate_url` removed: declared, read by nothing, so they
  were fake gates. A stream-only upstream aggregates in its own `complete()`.
- `engrix_router.subscribers/` gained the `__init__.py` every other layer already had, and
  `tests/test_hygiene.py` now walks layer *directories* instead of scanning existing
  `__init__.py` files -- the old form could not fail for a file that was never there.
- The log file sink the env vars promised is real: `EROUTER_LOG_FILE`,
  `EROUTER_LOG_FILE_MAX_BYTES`, `EROUTER_LOG_FILE_BACKUPS` drive a `RotatingFileHandler`
  (previously read into config and ignored), proven by a subprocess test because
  `logs.configure()` is a process singleton.
- Adapters whose protocol is not public live in ONE private distribution (ADR-0002) --
  one module or package per vendor. Per-vendor repos were dropped: discovery
  returns a list, so the only thing extra repos buy is more CI and more version pins.

### Fixed

- **No `+2000` token inflation.** The reference added 2,000 tokens to prompt and total in every
  client-visible `usage` block, which inflated the caller's ledger by 2,000 tokens per call.
  Usage reported to the client is the upstream number; when the upstream sends none, the
  estimate is flagged through `usage_source` instead of padded.
- **Replay is re-signed per attempt.** `build_request()` is pure and is called again for every
  candidate, so `requestId`, timestamp and signature are fresh on each try; reusing an
  identical signature hangs or is rejected by replay-sensitive upstreams.
- **A provider singleton holds no per-request state.** Providers are instantiated once per
  provider id and shared by concurrent requests; credentials, connection id, tier and session
  material travel as arguments or are read back off the request, instead of overwriting another
  account's state as soon as round-robin touches a second connection.
- **httpx client cache keyed by `(proxy, loop)`, not by proxy alone.** A cached client is bound
  to the event loop that created it, so a second loop (test client, a script calling
  `asyncio.run()` twice) reused a foreign client and failed on `aclose()` with
  `Event loop is closed`.
- An upstream 404 stays a 404: unknown models are not mapped to 403, which in the consuming
  client means "fatal" and benches an API key for 24 hours.
- Plain 4xx responses do not lock an account by default (`health.lock_plain_4xx = false`), so a
  client's bad request cannot self-inflict a outage across all accounts.
- Error responses are recorded, not only successes — the reference wrote usage history on
  `status ok` only, which made a quota-draining failure invisible in the database.
- Log and trace paths redact credential headers; the equivalent masking in the reference was
  commented out.
