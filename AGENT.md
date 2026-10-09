# AGENT.md — rules for anyone (human or agent) working in this repository

Read this before changing code. Every rule below is enforced by a test in `tests/`, so
"the tests say" is not advice, it is the build.

## What this project is

A local, self-hosted API gateway in front of LLM vendors: one canonical shape in the
middle, OpenAI and Anthropic inbound, N vendors outbound. It is a **translator**, not a
brain: a client must be able to configure anything it would configure against the vendor's
own API. Decisions that belong to the vendor stay with the vendor.

## The six rules that survive any refactor

1. **Layering.** `core < storage < transport = identity = auth < providers = accounts <
   routing = hooks = subscribers = services < pipeline < api = testing < web`. Nothing
   imports upward, and these siblings never import each other: `{providers, accounts}`,
   `{routing, hooks, subscribers, services}`. Enforced by `tests/test_layering.py` (AST,
   no dependencies).
2. **One decision, one owner.** Selection strategy lives in `routing/selector.py`, not in
   `accounts/connections.py`. Status-code policy lives in `core/errors.py`, not per
   provider. Usage merging lives in `subscribers/usage.py`. If you add a second place that
   decides the same thing, you have introduced the bug class this repo exists to remove.
3. **A vendor is one file.** Adding a provider means one new module in
   `src/engrix_router/providers/` declaring `DEFINITIONS` (and optionally
   `PROVIDER_CLASS`) — the registry scans folders, entry points and
   `EROUTER_PROVIDERS_PATH`; there is no import list to edit and no third file to touch.
   `if provider == "..."` outside `providers/` is a rejection.
4. **No per-request state on a provider instance.** Providers are singletons shared by
   every concurrent request and every account. Anything request- or connection-scoped goes
   through arguments (`transform_request(body, *, creds, stream)`), never `self`.
5. **One vendor, one name token, in every layer** (ADR-0004). `openai` and `anthropic` are
   spelled the same in `core/formats/`, `api/` and `providers/`; transport words
   (`_sse`) and endpoint words (`_messages`) do not belong in a module name, because the
   module owns the whole format. A reader should be able to guess the path.
6. **Conservative on egress, loud on failure.** Loopback bind by default, admin API
   fail-closed without `EROUTER_ADMIN_TOKEN`, budget and rate limiting refuse *before*
   sending, protocol drift freezes upstream instead of hammering, and a provider that
   fails to import is reported in `/health` — never silently absent.

## Language (ADR-0000)

Placement decides the language, and `tests/test_language_policy.py` checks it via the AST:

* **English** — identifiers, docstrings, log lines, client-visible and operator-visible
  strings, settings documentation, the dashboard, and everything in `docs/`, `README.md`,
  `SECURITY.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `AGENT.md`.
* **Indonesian** — `#` comments inside code, and `knowledge/` (gitignored kitchen notes).

The guard scans the shipped surfaces: `src/engrix_router/**` and `scripts/**` (both their
docstrings and their string literals), plus `docs/**.md` and `README.md` as text. Tests are
outside it on purpose: a test's note is for the contributor reading that test.

The AST never sees comments, which is exactly why this works. `DEFERRED_*` registries in
that test are shrink-only: they are empty now, and adding a file to them is a policy
violation, not a TODO.

## Public versus private (ADR-0002)

This repository is public and must never contain reverse-engineered vendor material:
vendor keys, custom alphabets, undocumented endpoint paths, impersonated identity headers.
Subscription-backed adapters live in separate private distributions and attach through the
`engrix_router.providers` entry-point group. `tests/test_no_private_material.py` enforces
the boundary and skips `data/` and `knowledge/` because those are gitignored.

## Schema

`src/engrix_router/schema.sql` is the only DDL source, and changing it for **existing**
databases requires bumping `SCHEMA_VERSION` in `storage/sqlite.py` *and* adding a
`MIGRATIONS` entry. No scattered `ALTER TABLE` anywhere. `tests/test_schema_migrations.py`
proves an old database is upgraded, that migrations are idempotent, and that a database
newer than the build is refused instead of silently corrupted.

## Settings

Two places, never both: `core/config.py` = needs a restart (paths, ports, env, size
limits, deadlines); `storage/settings.py` = operator-changeable at runtime (routing,
cooldowns, TTLs, budgets, retention). A vendor package registers its own keys with
`settings.register_defaults({...}, owner="<provider id>")` — the key prefix is the vendor
id, and core's `DEFAULTS` stays vendor-free. Never store a secret in the settings table.

## How to work here

```bash
pip install -e .[dev]
python -m pytest -q                 # offline suite
python scripts/verify_slice.py      # 39 end-to-end checks, upstream stubbed, no network
```

Run the tests after **every** edit, not at the end. Several rules above are only
mechanically true — a stale reference or a docstring that drifted is a build failure, and
finding that out an hour later costs an hour.

## Non-negotiables when something cannot be verified

* Do not invent a shape you have not measured. An unverified path stays unimplemented, and
  the README/ADR says why (examples: a private adapter's PAT exchange, Anthropic image blocks).
* Do not report a number you did not produce. A claim of "tested" without a runnable
  artifact is worse than admitting the gap.
* Do not rename, move, or delete files you did not write without saying so explicitly.
* Never log or store a token, key, or credential body. `REDACT_HEADERS` exists for a reason,
  and client keys are stored hashed.
* Keep scope: build the component that was asked for. Depth on the contracts (routing,
  tracing, breakers, error mapping) is wanted; growing the footprint is not.

## Attribution

Author, owner, and decision-maker in any repository-visible text is the brand **Engrix**.
