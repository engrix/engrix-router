# Contributing

Short version: read [README.md](README.md) and `docs/adr/` first, keep the boundaries they
describe, and open a pull request. The rules below are enforced by tests, not by review mood.

## Set up and run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .[dev]
python -m pytest -q                                 # the whole suite, all offline
python scripts/verify_slice.py                      # the vertical slice, 39 checks
```

`scripts/verify_slice.py` needs **no outbound network**: it monkeypatches the provider send
methods, so what it exercises is the gateway path (auth -> admission -> routing -> dispatch
-> trace -> usage -> rollup -> read API). It writes `data/verify_slice.json` as evidence.
Everything in `tests/` runs the same way, with httpx stubbed. Nothing here burns vendor
credits, and a test that reaches a real vendor is a bug in the test.

Python 3.11+ (CI runs 3.11 and 3.12).

## Layout rules

**A new provider is one new file** in `src/engrix_router/providers/` that declares:

```python
DEFINITIONS = (ProviderDef(...),)        # required to be a vendor
PROVIDER_CLASS = MyProvider              # optional; defaults to OpenAICompatibleProvider
```

and nothing else. The registry **scans** `providers/`, installed entry points in the
`engrix_router.providers` group, and `EROUTER_PROVIDERS_PATH` — there is no import list to
edit, and that is deliberate: a hand-maintained list is how a provider silently disappears.
`engine`, `routing`, `api` and `web` are never touched to add a vendor. See
`docs/ADDING_A_PROVIDER.md`.

- `if provider == "..."` outside `providers/` is a **rejection**, not a style comment.
  Dispatch goes through `providers/registry.py`, and vendor codes go in `core/errors.py`.
- A provider instance is a singleton per provider id, shared by every concurrent request:
  per-request state must not live on `self`, and `build_request()` must stay pure (it is
  re-called for every retry attempt).
- Providers whose protocol is not public are shipped in a separate private distribution and
  attach through the entry-point group.

## Language policy (ADR-0000)

Placement decides the language:

- **English** — identifiers, docstrings, log lines, client-visible error messages, UI strings,
  operator-facing settings documentation, and every public document (`README.md`, `docs/**`,
  `CHANGELOG.md`, `SECURITY.md`, this file).
- **Indonesian** — `#` comments inside code, because the owner reads and maintains this code.

Enforced by `tests/test_language_policy.py`, which parses strings and docstrings (the AST
never sees comments) and treats existing debt as a registry that may only shrink: adding an
Indonesian docstring or operator-facing string to a file that is not already listed fails.

## Layering (ADR-0003)

A module may import only from its own layer or below, and siblings in the same band may not
import each other. Ranks, as enforced by `tests/test_layering.py`:

```
core 0 < storage 1 < transport = identity = auth 2 < providers = accounts 3
     < routing = hooks = subscribers = services 4 < pipeline 5 < api 6 < web 7
```

Sibling groups that must stay independent: `{routing, hooks, subscribers, services}` and
`{providers, accounts}`. One decision, one owner.

## Schema changes

`src/engrix_router/schema.sql` is the only DDL file, and it describes **new** databases.
Changing it for **existing** ones is a two-part rule: bump `SCHEMA_VERSION` in
`storage/sqlite.py` **and** add a matching entry to `MIGRATIONS` in the same file. A DDL
change without both is a silent break for everyone whose database is already on disk.
Each migration step must be safe to run twice. `tests/test_schema_migrations.py` covers it.

Settings keys follow the same "one place" idea: a new runtime setting is one entry in
`DEFAULTS` (`storage/settings.py`); a provider package registers its own keys through
`settings.register_defaults(...)` instead of editing core.

## Never add reverse-engineered vendor material

Vendor keys, custom encoding alphabets, undocumented endpoint paths and impersonated
client-identity headers do not go into this repository at any point — not in code, not in
docs, not in a test fixture. `tests/test_no_private_material.py` fails when they appear, and
`data/`, `knowledge/` and `credentials/` are gitignored for the same reason. That material
lives in `engrix-router-providers`, one private distribution holding every such adapter.

## Certificate

By submitting a pull request you certify that you wrote the change or otherwise have the
right to submit it under this project's Apache-2.0 license, and that you may contribute it.
