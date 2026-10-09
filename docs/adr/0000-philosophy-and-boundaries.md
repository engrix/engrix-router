# ADR-0000 — Philosophy and boundaries

- Status: Accepted
- Date: 2026-10-09
- Deciders: owner (Engrix), agent
- Evidence: measured against 9router v0.5.95 and a live Qoder account (see `knowledge/` in this working tree — private by design, ADR-0002)

## What this gateway is

**A translator. Full stop.** A client (an agent loop, an SDK, a script) points at this
gateway instead of at a vendor, sends an OpenAI- or Anthropic-shaped request, and gets
back what it expects, with multi-account rotation, health, accounting and trace on top.

Everything below follows from that one sentence.

## Rules

### 1. The gateway never changes the meaning of a request

No prompt injection, no skills/prompt packs, no guardrails, no content rewriting, no
response caching that changes answers. Those were all considered and **rejected**:

- `hooks/skills` — rejected. The client owns its prompt. If the gateway edits it, the
  client's A/B experiments, token budgets and eval numbers become lies.
- `hooks/guardrails` — rejected. That's a product layer, not a translator.
- `hooks/cache` — rejected as a name. Response caching makes answers stale and breaks
  the accounting (you bill for work you did not do). Catalog and quota caches exist,
  and they live with their owner (`providers/` mirror + `accounts/limits`), not in a
  hook.

Kept, because they only decide *whether* to send, never *what* to send:
`hooks/ratelimit` (short window) and `hooks/quota` (per-user daily budget).

This is enforced by a contract test, not by this paragraph: no hook may mutate
`messages`, `system`, or `tools` (ADR-0001).

### 2. Config comes from the client, and the client must not know it talks to a gateway

**The gateway is transparent: a client configures exactly as if it were pointed at the
provider's own API.** The client owns model choice, sampling, tool schema, token limits
and reasoning effort; the gateway maps them to what the vendor understands and reports
what the vendor cannot honour. It never adds, edits, or reinterprets intent.

That is why `temperature`, `max_output_tokens`, `reasoning_effort`, `tools` and `stop`
are pass-through fields in the canonical format (ADR-0001) instead of gateway settings:
the moment the gateway owns a knob, the client's config stops being the client's.

Silent reinterpretation is the failure mode we came from — 9router synthesises HTTP 403
for vendor quota blocks, which benches the caller's API key for 24h.

### 3. Two inbound formats, N vendors

`2 + N`, never `2 × N`: inbound adapters produce one canonical request (ADR-0001), each
provider maps canonical → native. A vendor field that does not fit the canonical shape
goes into the vendor's own folder, never into `core/`.

### 4. What is deliberately out of scope

- **`clients/` package** — none. `claude_code`, `qoder`, `codebuddy` are providers
  (upstream types), not clients. Clients call the gateway.
- **Tool execution and MCP inside the gateway** — none. Tool calls are pass-through. If
  an agent loop is ever needed, it is a separate service that calls this gateway as an
  ordinary client, and it needs its own ADR first.
- **Embeddings, Responses API, batch** — none. Two REST surfaces only:
  `/v1/chat/completions` and `/v1/messages`.
- **`routing/topology/` and `ml/` internals** — placeholders. Empty folders with an
  `__init__.py` and a README line saying "no design yet". Do not let them look finished.

## Language policy (this is a public repository)

Placement decides the language. It is enforced by `tests/test_language_policy.py`, not
by goodwill.

| Surface | Language | Reason |
|---|---|---|
| identifiers, function/class/variable names, DB columns, settings keys, env vars | English | API surface |
| docstrings (module, class, function) | English, short | they surface in `help()`, IDE tooltips, generated docs |
| `#` comments inside code | **Indonesian** | the owner reads and maintains this code |
| log line strings (console, ring buffer, `/api/logs`) | English | operators pipe them to tools |
| dashboard/UI strings | English default, Indonesian available **as data** (`web/strings/en.json`, `id.json`) | no hard-coded bilingual duplication |
| client-visible error `message` | English, plus a machine-readable `code` | SDK callers branch on `code` |
| `README.md`, `docs/**`, ADRs, `CHANGELOG.md`, `LICENSE`, `SECURITY.md` | English, single source | 12 hand-maintained translations go stale and a wrong sentence about limits/ToS is someone's banned account |
| `knowledge/**` (RE reports, experiments, vendor data) | Indonesian, **gitignored forever** | kitchen drawer |
| commit messages | English | public history |

Where a comment needs a long "why", the why goes into an ADR and the comment points at
it: `# reason: ADR-0004 (replay of an identical signature hangs the upstream)`.

## Naming (non-negotiable)

- Files and packages: `snake_case` (`z_ai`, `claude_code`). Reports: `UPPERCASE_SNAKE`.
  Tests: `test_` prefix.
- No private abbreviations (`gw`, `psd` is tolerated only where it mirrors a vendor's
  own field name).
- One concern = one file, and one owner per decision. If two modules can decide "which
  account do I use", one of them is wrong (`routing/selector.py` owns it;
  `accounts/pool.py` only lists candidates).
- Extension points are data, not `if/else`: a new provider/hook/subscriber/storage
  backend/flow is a new folder that a registry discovers. `if provider == "…"` outside
  `providers/` fails review.

## Consequences

- The gateway is boring on purpose. Boring survives vendor changes.
- Anything that would make it smarter than the client is rejected here, so the rejection
  does not have to be re-litigated per feature.
- A public repo with subscription-based vendors has ToS exposure: the boundary is
  ADR-0002, not a disclaimer alone.
