# ADR-0003 — Layer map (what moved, what owns what)

- Status: Accepted
- Date: 2026-10-09
- Enforcement: `tests/test_layering.py` (AST, no external dependency). `import-linter`
  is the `[dev]` extra belt for CI; the test exists because the rule must hold on a
  fresh clone where nothing is installed yet.

## Rank table

```
core(0) < storage(1) < transport(2) = identity(2) = auth(2)
        < providers(3) = accounts(3)
        < routing(4) = hooks(4) = subscribers(4) = services(4)
        < pipeline(5) < api(6) = testing(6) < web(7)
```

Two sibling bands may not import each other: `{providers, accounts}` and
`{routing, hooks, subscribers, services}`.

`testing` sits with `api` because the contract suite drives the full pipeline (it has
to, or it proves nothing) while nothing below is allowed to know it exists.

## Where the old `router/` package went

| old | new | why |
|---|---|---|
| `router/contracts.py` | `core/types.py` | pure data + Protocol; foundation |
| `router/errors.py` | `core/errors.py` | taxonomy is data, no I/O |
| `config.py` | `core/config.py` | env/paths only |
| `ids.py` | `core/ids.py` | id primitive |
| `router/sse.py` | `core/formats/openai.py` | OpenAI frame vocabulary (it briefly lived as `openai_sse.py` — renamed by ADR-0004). ADR-0001's "one pipeline, N wire formats" landed as `pipeline.runner.stream_frames()`: the runner yields canonical frames and each inbound format renders its own bytes (`api/openai.py` → OpenAI SSE, `api/anthropic.py` → Anthropic events) |
| `db.py` | `storage/sqlite.py` | persistence |
| `router/settings.py` | `storage/settings.py` | **runtime settings overlay is stored state.** `core/config` stays pure data/env: `core` is rank 0 and must not import `storage` |
| `router/client_keys.py` | `identity/api_keys.py` | "who calls the gateway" is identity, not accounting |
| `router/proxy.py`, `router/url_guard.py` | `transport/proxy.py`, `transport/url_guard.py` | egress policy |
| `providers/http_client.py` | `transport/http_client.py` | providers were importing their own transport from inside the provider band |
| `router/connections.py` | `accounts/connections.py` | instance rows + credential loading |
| `router/health.py` | `accounts/health.py` | cooldown/locks/state machine |
| `router/quota.py` | `accounts/limits.py` | upstream quota per account (ADR-0000 keeps this separate from user budget) |
| `router/catalog.py` | `accounts/catalog.py` | per-connection model cache; the vendor mirror itself is the `provider_catalog` table |
| `router/routing.py` | `routing/selector.py` | selection strategy |
| `router/budget.py` | `hooks/budget.py` | user/key/lane daily budget (admission) |
| `router/engine.py` | `pipeline/runner.py` | the only place that sequences stages |
| `router/logger.py` | `core/logs.py` | see below |
| `router/trace.py` | `subscribers/trace.py` | per-request audit trail rows |
| `router/usage.py`, `router/pricing.py` | `subscribers/usage.py`, `subscribers/pricing.py` | cost/ledger accounting |

## Deviations from the agreed sketch (deliberate, recorded)

1. **`accounts/connections.py`, not `accounts/models.py`.** `models.py` already means
   "list of models" inside every provider folder; two meanings for one filename in one
   repo is the ambiguity ADR-0000 forbids.
2. **`hooks/budget.py`, not `hooks/quota.py`.** Two things were going to be called
   "quota": user budget (`hooks/`) and upstream limit (`accounts/limits.py`). Naming one
   `budget` removes the collision; ADR-0000's separation rule still holds, one word
   sharper.
3. **Logger is `core/logs.py`, not `subscribers/logging/`.** Measured: `providers`,
   `accounts`, `identity` and `transport` all emit log lines, so keeping the emit
   primitive in rank-4 would have forced 7 upward imports — exactly what ADR-0000
   prohibits. Emitting is foundation; *consuming* is a subscriber. The console-tail
   SSE endpoint stays in `api/admin_observability.py` and reads the ring from
   `core/logs`. If a real event bus arrives, `subscribers/logging/` becomes the
   consumer of those events, not the sink.
4. **No `routing/resolver.py` yet.** The pipeline already resolves
   `provider/model → ProviderDef` through `providers.registry` (rank 5 → 3, legal), so a
   resolver module today would be a pass-through file with no job. It appears when
   alias/combo sets (multi-provider aliases) exist.
5. **`accounts/` never resolves a provider.** `accounts.catalog.models_for()` and
   `accounts.limits.get_quota()` now take the **provider instance** as an argument
   instead of calling `providers.registry` internally (they used to, hidden behind a
   lazy in-function import to dodge a circular import). Callers in `api/` and
   `pipeline/` resolve it. Two rules buy that refactor: sibling independence, and no
   dependency hidden inside a function body.

## Ownership of contested decisions

| decision | owner | explicitly not owned by |
|---|---|---|
| which connection serves a request | `routing/selector.py` | `accounts/connections.py` (lists candidates only) |
| `qd/qfmodel` → provider + model | `providers/registry.py` (called by pipeline) | `accounts/`, `core/` |
| status code the client sees | `core/errors.py` policy table | providers (they raise `UpstreamError` only) |
| canonical → native payload | `providers/<vendor>.py` | `core/`, `api/` |
| daily budget admission | `hooks/budget.py` | `accounts/limits.py` |
| upstream quota reading | `accounts/limits.py` | `hooks/budget.py` |
| short-window traffic shape guard | `hooks/ratelimit.py` | `hooks/budget.py` (accounting is day-scoped) |
| inbound wire format (OpenAI SSE vs Anthropic events) | `api/<format>.py` + `core/formats/<format>.py` | `pipeline/` (one frame stream, N renderings) |
| vendor cost signals (`billable`, `credits`) | `subscribers/usage.py` → `subscribers/trace.py` | providers (they hand over the raw usage block) |
| which providers exist at runtime | `providers/registry.py` + entry points + `EROUTER_PROVIDERS_PATH` | every other module (ADR-0002) |
| a vendor's own settings keys | the vendor's package, via `settings.register_defaults(owner=...)` | `storage/settings.py` (core defaults only) |

## Modules added after the first map (2026-10-09)

| module | layer | what it owns |
|---|---|---|
| `core/formats/anthropic.py` | core | the Anthropic Messages shape, both directions (inbound client format AND outbound vendor protocol), so the two cannot drift. Renamed from `anthropic_messages.py` by ADR-0004 |
| `api/anthropic.py` | api | `POST /v1/messages`: translate, run, render Anthropic events |
| `providers/anthropic.py` | providers | Anthropic-compatible upstream: native payload, event decoding, mandatory `max_tokens` |
| `providers/_template.py` | providers | copy-paste starting point; leading underscore keeps it out of discovery |
| `hooks/ratelimit.py` | hooks | sliding 60s windows per client key and per provider |
| `services/quota_sync.py` | services | the only background job that exists: vendor quota snapshots |
| `testing/contract.py` | testing | the shared provider suite exported to provider packages (ADR-0002) |

The Qoder adapter left this repository entirely when ADR-0002 was executed; nothing in
the core imports it, and `tests/test_no_private_material.py` keeps it that way.

Not built, and not faked: `routing/topology/`, `ml/`, `docs/providers/`,
token-refresh and periodic-probe services. They have no design yet, so they have no
files — an empty folder that looks architectural is worse than a sentence in the README.
