# ADR-0002 — Public core, private subscription providers

- Status: Accepted
- Date: 2026-10-09
- Deciders: owner (Engrix)
- Precondition: this repository is **public** and has **no git history yet** — the split
  costs one move today and a history rewrite later. Do it now.

## Decision

Two artifacts, one contract.

```
engrix-router            (PUBLIC, this repo)
  core, pipeline, routing, accounts, hooks, subscribers, services,
  storage, transport, auth (generic flows), identity, api/{openai,anthropic,admin},
  providers/base.py, providers/openai.py, providers/anthropic.py,
  providers/_template.py, testing/contract (the suite), docs/, LICENSE, SECURITY.md

engrix-router-providers  (PRIVATE, ONE distribution for every reverse-engineered vendor)
  <vendor>.py per adapter (qoder today), its scripts, fixtures, RE notes
```

One repo, not one-repo-per-vendor, and the reason is operational: N private repos means
N pyprojects, N CIs, N version pins and N places to forget. Discovery does not care which
distribution a provider came from -- the entry-point group already returns a list -- so the
only thing per-vendor repos would buy is a bigger chance of shipping a half-migrated
adapter. The boundary that matters is public versus private, not vendor versus vendor.
(`engrix-router-qoder` was the first shape of this decision and was folded into
`engrix-router-providers` the same day; see the implementation note below.)

A public gateway that ships reverse-engineered material (vendor RSA public key, custom
base64 alphabet, undocumented endpoint paths, impersonated client identity headers) is
that material, published. It also hands the vendor a one-file diff to break us and gives
the owner a ToS problem that a README disclaimer does not fix. Keeping the generic core
public is what makes the project useful to others; keeping the subscription adapters
private is what makes them ours.

## Discovery mechanism

Registry scans **both** sources and lazy-loads (name first, import on use):

1. Built-in: `engrix_router/providers/*/` in this repo.
2. Entry points: distribution group `engrix_router.providers`
   (`importlib.metadata.entry_points(group="engrix_router.providers")`) — how a private
   package attaches without touching our code.
3. Filesystem override for development: `EROUTER_PROVIDERS_PATH` (path list). Lets the
   private repo be run against a checkout without building a wheel.

Each provider folder exposes `PROVIDER` (definition + class). Requirements:

- **One broken provider must not take the others down.** Import errors are captured per
  provider, recorded in the registry, excluded from routing, and surfaced in
  `/api/state` as `provider_load_errors` — loud, never silent (the failure mode we came
  from: a hand-maintained registry that silently loses a provider).
- Duplicate model prefix across any two sources = hard error at startup, not "first one
  wins".
- `if provider == "…"` outside `providers/` is a review rejection (ADR-0000).

## Shared contract test suite

`engrix_router.testing.contract` (public) exports one suite: plain request, streaming,
tool call, error mapping, usage fields, and the two invariants — hooks do not mutate
content (ADR-0000), and provider output decodes to canonical events (ADR-0001).

Private provider repos run the same suite in their CI against fixtures, and against the
live vendor only in their own (private) environment. That is how "public core" stays
honest: we own the contract, vendors must satisfy it.

## Never in the public repository

| item | why |
|---|---|
| `knowledge/**` (RE reports, wire data, experiments) | Indonesian kitchen drawer; contains vendor internals and proxy credentials |
| `data/**` (live artifacts, request bodies, traces) | real `user_id`/`machine_id`, quota numbers, prompt content |
| `credentials/**`, `.env*` | secrets |
| vendor public keys, custom alphabets, undocumented paths, identity header sets | live only in private provider packages |
| account ids / tokens in examples | use obvious placeholders (`<QODER_DEVICE_TOKEN>`) |

Enforcement, not intention: `.gitignore` (already correct), `pre-commit`, and a secret
scan (gitleaks) in CI. First commit happens only after this move is done.

## ToS and risk posture (public README, non-negotiable wording)

- "Unofficial client. Not affiliated with any vendor. Subscription-backed providers may
  break at any time and may violate the vendor's terms of service; you use them at your
  own risk."
- No account creation, no credential harvesting, no captcha/turnstile bypass, no
  detection evasion. If something looks like that, it does not ship.
- Conservative defaults for egress: single account until the owner opts into a pool,
  budget guards on, drift kill-switch documented (the gateway already freezes upstream
  on protocol drift instead of hammering).
- `SECURITY.md`: credential/ToS problems go to a private channel, not public issues.

## License

Apache-2.0 (patent grant; friendlier than MIT if a vendor ever forks and re-publishes).
Files that must never be relicensed out of our control (private adapters) live outside
this repo entirely, so the license question stays simple.

## Implementation note (executed 2026-10-09)

The split happened, then was simplified the same day. `engrix-router-providers` is ONE
sibling private distribution (`src/engrix_router_providers/qoder.py` + its scripts and
tests) that attaches to the public core; the core contains no vendor material and nothing
imports that package (`tests/test_no_private_material.py` enforces both). The first cut was
a per-vendor repo (`engrix-router-qoder`); it was folded into one because discovery never
needed a repo boundary and per-vendor repos only multiply CI and version pins. Four details
differ from the decision text above, each for a concrete reason:

1. **The contract is `DEFINITIONS` + optional `PROVIDER_CLASS`, not `PROVIDER`.** The
   decision text sketched a folder exposing a single `PROVIDER` object. In practice the
   registry already resolved "(one definition, many prefixes) + (one shared class)" and
   a second spelling of the same idea would mean two ways to register a vendor. One
   contract, one name. Built-in modules, built-in packages, entry points and
   `EROUTER_PROVIDERS_PATH` all feed the same `_register()`.
2. **Load errors surface on `/health`, not `/api/state`.** There is no `/api/state`
   route; `/health` already answers "what is this process doing right now", is the thing
   a supervisor and the Docker healthcheck poll, and adding a second status endpoint
   would be a second source of truth. `providers` and `providers_failed` are in its body
   and every failed source is also logged at startup.
3. **`EROUTER_PROVIDERS_PATH` imports under the real dotted name when `sys.path` allows
   it.** Loading the same file twice (once as a path provider, once as a package module)
   produced two provider classes, so a test that patched one was not testing the one the
   registry routed. `_import_from_path()` prefers the real name.

The shared contract suite ships as `engrix_router.testing.contract.ProviderContract`.
It is run in this repo against the two built-in providers (`tests/contract/`) and in the
private repo against Qoder (`tests/test_contract_qoder.py`) — which is the point: the
same assertions decide whether an adapter is allowed to attach.

**Amended 2026-10-09:** the split is executed and the repository now has git history
(initial commit = post-split tree), so the precondition above no longer holds; it is kept
as the record of why the split happened before the first commit.

`LICENSE` was fetched from `https://spdx.org/licenses/Apache-2.0.txt` because
`www.apache.org` is unreachable from the owner's machine; it is the same canonical text
(9 sections + appendix), but re-download from apache.org before the first public push if
byte-exactness matters.
