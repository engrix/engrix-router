# Adding and debugging a provider

The main rule (the owner's requirement, and the reason this tree does not become a knot of
wires):

> **Adding a vendor = one new file in `src/engrix_router/providers/` (plus one contract test
> class). Done.** The registry finds it. `pipeline`, `routing`, `api`, `web` are untouched.
> Debugging vendor X happens inside X's file, not by jumping between `y` and `z`.

What exists as a worked example in this repository:

| file | why it is the example |
|---|---|
| `providers/_template.py` | the copy-me starting point. It carries real code but is **not routable**: the scanner skips any name starting with `_` |
| `providers/openai.py` | the default class, plus `build_node_definition()` which turns a runtime `nodes` row into a `ProviderDef` |
| `providers/anthropic.py` | a vendor whose wire protocol differs from OpenAI: different auth headers, a mandatory parameter, a typed event stream, a non-stream object that is not a `chat.completion` |
| `providers/base.py` | every hook a vendor may override, and why each one exists |

A subscription vendor whose protocol is not public is **not** in this repository: it lives in a
separate private distribution and attaches through the discovery mechanism in section B. See
[ADR-0002](adr/0002-public-core-private-providers.md) and section F.

The three routes below are ordered cheapest first.

## A. No code at all: a runtime node

A `nodes` row is a provider an operator defines in the database, served by the generic
OpenAI-compatible class:

```bash
curl -s -H "authorization: Bearer $EROUTER_ADMIN_TOKEN" -H content-type:application/json \
  -X POST http://127.0.0.1:8450/api/nodes/validate \
  -d '{"base_url":"https://api.example.com/v1","api_key":"<PROVIDER_KEY>"}'
# -> {"ok":true,"stage":"models","base_url":"...","models_seen":41,"sample":["..."]}

curl -s -H "authorization: Bearer $EROUTER_ADMIN_TOKEN" -H content-type:application/json \
  -X POST http://127.0.0.1:8450/api/nodes \
  -d '{"name":"Example Cloud","prefix":"ex","base_url":"https://api.example.com/v1"}'

curl -s -H "authorization: Bearer $EROUTER_ADMIN_TOKEN" -H content-type:application/json \
  -X POST http://127.0.0.1:8450/api/connections \
  -d '{"provider":"ex","name":"acct-1","api_key":"<PROVIDER_KEY>"}'
```

`prefix` is the provider id, so the model is called as `ex/model-name`. Prefixes are validated
(lowercase letters, digits, `-`, `_`, 2-32 characters) and unique together with the node name: a
collision is refused with `409`, because a shadowed prefix is silent misrouting.

Automatic guards: the SSRF check in `transport/url_guard.py`, `nodes.validate_before_create` (a
real `GET /models` before the row is stored), `nodes.allow_private_urls=false`. `api_type` maps
`chat` to `/chat/completions` and anything else to `/responses`; `embeddings` and the
`anthropic-compatible` value of `type` are recorded but **not dispatched** -- a node is always
served by `OpenAICompatibleProvider`. If the upstream speaks a different protocol, write a provider.

`POST /api/connections` also runs the zero-token probe unless you send `"probe": false`; a failed
probe does not fail the create, it is attached to the response. The model catalogue the client
sees comes from the upstream itself, read through `accounts/catalog.py`.

## B. Built-in declaration: data only

Copy `providers/_template.py` to `providers/myvendor.py` and edit the declaration. That is the
whole wiring.

```python
from engrix_router.core.types import ProviderDef, TransportSpec
from engrix_router.providers.openai import OpenAICompatibleProvider

DEFINITIONS = (
    ProviderDef(
        id="myvendor",                       # the model prefix: myvendor/model-x
        category="apikey",
        display_name="My Vendor",
        aliases=("mv",),                     # extra prefixes, all claimed by this one file
        transport=TransportSpec(
            base_url="https://api.example.com/v1",
            default_model="model-x",
        ),
    ),
)

# Optional. Omit it and the registry uses OpenAICompatibleProvider, which is the right answer
# for any upstream that already speaks /v1/chat/completions.
PROVIDER_CLASS = OpenAICompatibleProvider
```

### The discovery contract

`providers/registry.py` scans three sources and never a hand-edited import list
([ADR-0002](adr/0002-public-core-private-providers.md)):

1. **built-in modules** (`providers/*.py`) **and built-in packages** (`providers/<name>/` with an
   `__init__.py`);
2. **installed entry points** in the distribution group `engrix_router.providers`;
3. **`EROUTER_PROVIDERS_PATH`**, an `os.pathsep`-separated list of directories holding provider
   modules or packages -- the development override that lets an external adapter run from a
   checkout with no wheel built.

Every source declares the same two module attributes, `DEFINITIONS` and the optional
`PROVIDER_CLASS`, so an external provider is written exactly like a built-in one. Folder-form
providers put those two attributes in the package's `__init__.py`; a directory without
`__init__.py`, or whose name starts with `_` or `.`, is not a provider.

Names the scanner always skips: `__init__`, `base`, `registry`, and anything starting with `_`
(which is why `_template.py` ships real code without becoming routable).

Two failure modes are errors, not opinions:

* **One broken source must not take the others down, and must not be silent.** An import failure is
  recorded per source (`registry.load_errors()`, keyed `source:name`), that provider is excluded
  from routing, every other provider keeps working, the errors are logged at startup and served by
  `GET /health` under `providers_failed`. A missing directory in `EROUTER_PROVIDERS_PATH` is
  reported the same way.
* **Two sources claiming one prefix is `DuplicatePrefix`, raised at startup** -- not "first one
  wins", because a shadowed prefix routes to a vendor the operator never picked.

`python -m pytest tests/test_registry_discovery.py` is the executable spec of both rules, and
`tests/fixtures/{good,broken,clash}/` are real provider files the registry loads.

## C. A vendor with its own protocol

Subclass `BaseProvider` and override only the hooks that are actually different. One file.

| hook | contract | when you need it |
|---|---|---|
| `transform_request(body, *, creds, stream)` | canonical (OpenAI-shaped) body -> the vendor's native payload dict | any field the vendor spells differently, any hoisting (`anthropic.py` moves `system` out of the message list), any parameter it requires (`anthropic.py` fills a documented `max_tokens` default because the upstream answers 400 without it) |
| `encode_body(payload, *, stream)` -> `(bytes, query_extra)` | the **final bytes** on the wire plus extra query parameters | byte-level framing or obfuscation. Rule: produce the bytes first, sign second -- a signature must be computed over exactly the bytes sent |
| `sign_request(request, creds)` -> `ExecRequest` | the last hook over the final body and URL | a per-request signature or a signature-style header set. Runs **per attempt**, so the id, timestamp and signature are always new |
| `unwrap_data(data)` -> chunk or `None` | one raw `data:` line -> one canonical chunk; `None` means "nothing to forward" | a vendor that wraps each frame in an envelope, or that streams typed events instead of deltas. An error inside the envelope must become an error, never an empty chunk that looks like a model that went quiet |
| `open_stream(request)` | async iterator of canonical chunks; `base.py` owns the SSE line loop, the first-chunk and stall watchdogs, the content-type guard and the error-frame raise | only when the framing is not `data:` lines at all -- then override `_open_stream_inner` (or call `super()` from your own `open_stream`) so the timeouts and the error translation are not lost |
| `complete(request)` -> `chat.completion` dict | the non-stream answer | when the upstream answers a different object shape (`anthropic.py` folds a `message` object into a `chat.completion`) -- or when the upstream **only** streams: aggregate the deltas and tool-call fragments here, because there is no `requires_stream` flag to honour today -- the knowledge that a vendor refuses non-stream lives with the vendor |
| `list_models(creds)` -> `[ModelSpec]` | the vendor's model catalogue | whenever the list is not the plain `{"data": [...]}` shape. This is what `probe()` and `GET /api/connections/{id}/models` use |
| `probe(creds, model_id)` -> `ProbeResult` | a cheap, zero-credit check that **never raises** | the default is `GET /models`; override it when the vendor can be checked another way, and never override it with a real chat: every chat spends credits. Declare `probe_tier` (`models_list` \| `one_token_chat` \| `key_exchange`) so the dashboard can say what a test costs |
| `fetch_quota(creds)` -> `[QuotaReading]` | the vendor's own quota numbers | when the vendor reports them: declare `transport.usage` (a `UsageSpec`: url, method, auth, dotted `mapping`, `reset_path`) and add `"usage"` to `features`. That is also what `services/quota_sync.py` iterates over and what feeds the **Vendor quota** card |

Declarative knobs on `TransportSpec` before you write code: `AuthSpec(kind, header, prefix,
extra_headers)` (bearer, a named header, or none -- auth style is data, so no service code branches
on the vendor), `chat_path` / `models_path` / `models_url` / `url_suffix` / `headers`,
`accept_encoding` (declare `identity` when a signed request must not be recompressed in transit: the
httpx default `gzip, deflate` has caused signed requests to be rejected with an error that looked
like a credential problem), `send_stream_options=False` and
`strip_params` for upstreams that reject unknown parameters, `retry` per status code,
`connect_timeout_s` / `stall_timeout_s`, `default_model`, `features`.

### Three rules that keep a vendor out of trouble

1. **No per-request state on `self`.** `registry.get_provider(prefix)` caches one instance per
   provider id and shares it across every concurrent request. `connection_id`, session material,
   tier, catalog keys must travel as arguments (that is why `transform_request` receives `creds`)
   or be read back off the request. A version of the first subscription adapter stored a connection
   id on the instance and would have overwritten one account's state as soon as round-robin used a
   second -- so the contract suite tests it (`test_two_builds_do_not_share_mutable_state`).
2. **`build_request()` must be pure, and re-signed per attempt.** The runner calls it again for
   every candidate, so ids, timestamps and signatures are fresh each time. For a replay-sensitive
   upstream this is a requirement, not hygiene: an identical signature is rejected outright
   (`403 code 103 Duplicate request` in the reference implementation) or answered with a silent
   hang, measured in our own probes. Purity is also what makes `EROUTER_DRY_RUN` possible.
3. **Error classes do not live in the vendor file.** Vendor-specific codes are mapped in
   `core/errors.py`, one place, because the status code the client sees is a gateway decision (see
   [ARCHITECTURE.md](ARCHITECTURE.md) §4). A provider raises
   `UpstreamError(status=..., vendor_code=..., kind=...)` and stops there.

### Vendor data: what belongs in code, what belongs in the database

| kind of data | where | why |
|---|---|---|
| URLs, paths, framing, the signing algorithm, payload enums | code (the vendor file) | identical for every account |
| the model catalogue: ids, context windows, thinking/tier config, enable flags, price factors | read from the vendor at run time; cached per connection by `accounts/catalog.py`; mirrored per account in `provider_catalog` when a vendor's catalogue is account-scoped | **never hand-written.** An invented catalogue is not rejected by the vendor -- it is accepted and quietly served by a cheaper model (measured as a silent downgrade in the ported project). What is not read from the vendor is not knowledge |
| behaviour knobs (`myvendor.some_knob`) | `settings.register_defaults(...)`, see below | changeable through the API without a restart |
| device or account identity (user id, machine id, header overrides) | `connections.psd_json`, per account | it is per-account data, not source code |

### Vendor knobs: `settings.register_defaults`

The public `DEFAULTS` registry in `storage/settings.py` holds **no vendor keys**. A provider package
registers its own at import time:

```python
from engrix_router.storage import settings

settings.register_defaults(
    {
        "myvendor.catalog_ttl_s": (3600, int, "how long the model catalogue is cached"),
        "myvendor.tier_policy": ("auto", str, "auto | default | max"),
    },
    owner="myvendor",
)
```

The rules the function enforces: every key must start with `"<owner>."` -- the prefix is what keeps
two adapters off one key and makes `grep` find the owner of a setting -- and registering an existing
key with a **different** default raises, because overwriting a default depending on import order
would change behaviour silently. Re-registering identical entries is fine, so a module can be
imported twice.

Once registered, the keys behave like any other setting: they appear in `GET /api/settings` (values
plus the schema with default, type and doc), they are writable through `PATCH /api/settings` or the
dashboard **Settings** tab, and `settings.get_int` / `get_bool` / `get_dict` read them live. A key
that was never registered raises `KeyError` on read -- an unknown knob is an error, not a `None`.

## D. Debugging a provider

1. `GET /health` -- provider count and `providers_failed` (per-source import errors). Public, no
   token, no secrets in the body.
2. `GET /api/state` -- providers with their prefixes, base URLs, whether they came from a node, the
   catalogue cache, the HTTP pools, health and the drift state.
3. `POST /api/connections/{id}/test` -- the zero-credit probe. Every provider has a default probe,
   so a new vendor is debuggable on day one instead of answering "test not supported". Dashboard:
   **Connections -> Test**.
4. `GET /api/connections/{id}/models?force=true` -- the upstream's own list, bypassing the TTL
   cache. Dashboard: the **Catalogue** button (context, tools, vision per model).
5. One real request, then `GET /api/usage/requests/{rid}` -- the stage rows: `client_in`,
   `openai_mid`, `provider_out`, `upstream_in`, `client_out`, plus `dry_run` when it applies.
   Credential headers are redacted; payloads are cut with explicit `truncated` + `original_bytes`
   markers. This replaces the reference implementation's unrotated multi-file request dump whose
   header masking was commented out. Dashboard: **Requests & traces**.
6. `GET /api/logs/stream?seconds=60` (or `GET /api/logs`) -- the live ring; error lines bypass the
   level gate. Dashboard: **Log**.
7. `POST /api/connections/{id}/test_models` with `{"allow_spend": true}` -- pings models through
   this gateway's own `/v1/chat/completions`, so the debug path and the traffic path run the same
   code. **This spends credits**, which is why `allow_spend` is mandatory (412 without it) and why
   the default is one model per call (`max_models`).
8. `EROUTER_DRY_RUN=true` -- **implemented**. The whole pipeline runs (resolve, admission, routing,
   `build_request`, trace) and the upstream is never contacted: a `dry_run` stage is written as step
   6, the trace row finishes with status `dry_run`, the budget reservation settles to 0, and the
   JSON answer carries `"engrix_dry_run": true`. Use it to diff a payload before spending anything.

## E. Testing a provider: the shared contract suite

`engrix_router.testing.contract` ships the suite every provider must pass -- built-in ones here, and
a private adapter distribution runs **the same suite** in its own CI. That is what keeps "public
core" honest: the invariants are tests an adapter cannot pass by accident.

```python
"""Contract suite for my vendor: canned upstream bodies, zero network."""
import json

from engrix_router.testing.contract import ProviderContract


class TestMyVendorContract(ProviderContract):
    provider_id = "myvendor"
    model = "myvendor/model-x"
    expected_content = "HELLO WORLD"
    expected_usage_total = 18
    chat_body = json.dumps({                                  # the vendor's own non-stream shape
        "id": "gen_1", "object": "chat.completion", "created": 0, "model": "model-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "HELLO WORLD"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}})
    stream_text = "".join(                                    # raw SSE bytes, provider-native
        f"data: {json.dumps(frame)}\n\n" for frame in [
            {"choices": [{"index": 0, "delta": {"content": "HELLO"}}]},
            {"choices": [{"index": 0, "delta": {"content": " WORLD"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                      "total_tokens": 18}}])
    models_body = json.dumps({"data": ["model-x"]})
    error_status = 429
    error_body = json.dumps({"error": {"message": "slow down"}})
```

Set `connection_kwargs` instead of relying on the default `{"api_key": "sk-contract"}` when the
adapter needs `provider_specific` (device identity for signing) or an `access_token`: the suite
creates a **real** `connections` row and loads credentials back through the real path, because a
hand-built dict proves nothing. `chat_content_type` is the content type the stub answers with for
`chat_body` (default `application/json`), and `tool_stream` / `expected_tool_name` /
`expected_arguments` carry the vendor's own tool-call fragments.

What the suite proves, and why each one is there:

* `build_request()` is offline-pure, and the caller's body is not mutated;
* two consecutive builds share no mutable state (the singleton rule);
* a stream decodes into canonical chunks -- `delta` dicts, legal `finish_reason` values, and the
  aggregated content equals `expected_content`;
* a split `tool_calls` fragment reassembles into valid JSON **by `index`** (ADR-0001's merge key),
  using the vendor's own bytes -- the invariant is the canonical shape, so each adapter proves it
  with its real wire format, not with a stub written to look like OpenAI;
* an error frame inside the stream raises instead of looking like a silent model;
* an HTTP error surfaces as `UpstreamError` carrying the vendor's status;
* `list_models()` returns `ModelSpec`s and `probe()` returns a `ProbeResult` without raising;
* usage totals survive (field-wise max across frames, never a naive sum, never padded);
* through the **full pipeline** (`runner.run_chat`), the caller's request body is byte-identical
  before and after the run, and the content the vendor returned is what the client receives -- the
  ADR-0000 no-hook-mutation invariant, asserted end to end rather than per hook.

`ProviderContract` is deliberately not named `Test*`: pytest must not collect the base class, only
subclasses that supply the fixtures. In-repo subclasses live in `tests/contract/`
(`test_openai_provider.py`, `test_anthropic_provider.py`).

Everything stays offline: `httpx` is stubbed by the `no_network` fixture, so no test reaches a
vendor and no test burns credits. A test that needs the network is a bug in the test -- live vendor
verification belongs to the distribution that owns the credentials, run in its own private
environment.

## F. What must never be added to this repository

[ADR-0002](adr/0002-public-core-private-providers.md) splits this project in two because shipping
reverse-engineered material **is** publishing it, and it hands the vendor a one-file diff to break
every adapter. Never commit here:

* a vendor's public key or key material of any kind;
* an obfuscation or custom encoding alphabet;
* undocumented endpoint paths;
* an impersonated client-identity header set;
* wire captures, RE reports, probe artifacts, vendor internals -- `knowledge/` and `data/` are
  gitignored and stay that way;
* real account ids, tokens, machine ids, quota numbers, or prompt content in examples and tests:
  use obvious placeholders (`<PROVIDER_KEY>`, `<PROVIDER_TOKEN>`).

`tests/test_no_private_material.py` scans the whole tree for those markers and fails CI -- including
this documentation and its fenced code blocks. So describe an adapter generically in public docs:
"a subscription adapter shipped in a separate private distribution", not its endpoints or its
signature recipe. If a decision genuinely belongs to a non-public protocol, the artifact is a
private `engrix-router-<vendor>` distribution with its own `providers/<vendor>/`, its own scripts,
its own fixtures and its own subclass of the contract suite, attaching through the
`engrix_router.providers` entry-point group.

Language policy ([ADR-0000](adr/0000-philosophy-and-boundaries.md)) is enforced the same way:
documentation, docstrings, log lines and client-visible messages are English; `#` comments inside
code are Indonesian. `tests/test_language_policy.py` regex-scans whole files, so an Indonesian word
in a code sample in a doc fails CI.

## Checklist before a provider is considered done

- [ ] `python -m pytest -q` green, including the contract subclass for your vendor.
- [ ] `python scripts/verify_slice.py` still 39/39 (it does not know your vendor; it proves the core
      did not regress).
- [ ] `python -m pytest tests/test_no_private_material.py tests/test_language_policy.py
      tests/test_no_stale_references.py -q` green -- the repository-boundary guards.
- [ ] `probe()` returns `ok=true` against real credentials, and the catalogue fills.
- [ ] One real chat through **HTTP**, not the class directly: the trace stages are complete, the
      reported `usage` equals the upstream numbers (no inflation), the answer and any tool call are
      readable. Where this is run matters: outside this repository, in the distribution that owns
      the credentials.
- [ ] Daily limit or pay-per-model applies: declare `transport.usage` + `fetch_quota()` and `"usage"`
      in `features`, then confirm the reading shows up in `GET /api/quota` and that
      `services/quota_sync.py` picks it up when `services.quota_sync_interval_s` is non-zero.
- [ ] Vendor cost signals (`billable`, `credits`) land in `requests` -- the contract suite plus
      `verify_slice.py` cover the plumbing; a vendor with its own field names needs its own signal
      assertion.
- [ ] Upstream rejects unknown fields: `send_stream_options=False` / `strip_params`.
- [ ] Upstream refuses non-stream: `complete()` aggregates.
- [ ] New vendor error codes were added to `core/errors.py`, not to `if`/`else` in the vendor file.
- [ ] Nothing in the vendor file is a per-request attribute on `self`.
