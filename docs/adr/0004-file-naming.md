# ADR-0004 — File naming: one vendor, one token, in every layer

- Status: Accepted
- Date: 2026-10-09
- Deciders: owner (Engrix), prompted by his question: "why do `openai_sse.py` and
  `anthropic_messages.py` have different shapes, and where is OpenAI's own file under `api/`?"
- Enforcement: `tests/test_no_stale_references.py` (the old names are now stale patterns) plus
  `tests/test_layering.py` for the folder rule.

## The problem, measured before fixing

Four modules carried four different naming logics for the same job:

| layer | was | what the file actually contains |
|---|---|---|
| `core/formats/` | `openai_sse.py` | chunk shape + SSE framing **and** `to_completion()` for non-stream |
| `core/formats/` | `anthropic_messages.py` | request/response translation **and** the event renderer |
| `api/` | `compat.py` | the OpenAI inbound route (`/v1/chat/completions`, `/v1/models`) |
| `api/` | `anthropic.py` | the Anthropic inbound route (`/v1/messages`) |
| `providers/` | `openai_compatible.py` | the generic OpenAI-shaped provider + node definitions |
| `providers/` | `anthropic.py` | the Anthropic-shaped provider |

Each name was locally defensible -- `sse` described what the first file did when it was written,
`messages` is the vendor's endpoint, `compat` was there before a second format existed -- and
together they made the tree unreadable. A reader (or an agent) looking for the OpenAI wire format
has to know that one layer calls it `openai_sse`, another calls it `compat`, and a third calls it
`openai_compatible`. The vendor token was the only stable thing, and it was being hidden.

## Decision

**A vendor/protocol token is spelled identically everywhere it appears.** The layer says what kind
of module it is; the filename says which protocol it is about. Nothing else.

```
core/formats/openai.py        api/openai.py        providers/openai.py
core/formats/anthropic.py     api/anthropic.py     providers/anthropic.py
providers/<vendor>.py         (private: engrix_router_providers/<vendor>/)
```

Rules that follow:

1. **Transport or endpoint words do not belong in the module name.** A format module owns the whole
   format: request shape, response shape, streaming. Naming it `_sse` or `_messages` describes one
   of its three jobs and lies about the other two.
2. **A role-name with no vendor token is only allowed while there is one role.** `compat.py` was
   fine when it was the only inbound format; the moment `anthropic.py` appeared next to it, the
   pair became un-guessable. If a file's name cannot be paired with its sibling, rename.
3. **Class names may be more specific than module names.** `providers/openai.py` exports
   `OpenAICompatibleProvider` because the class genuinely serves any OpenAI-compatible upstream,
   including runtime `nodes` rows. The module is the protocol family; the class is the
   implementation. That asymmetry is intentional and stays documented here.
4. **Folders are layers, vendors are files, and a folder without `__init__.py` is a bug.** Every
   layer folder is a real package with a one-line docstring stating what the layer owns
   (`subscribers/` was missing one -- `tests/test_hygiene.py` now walks directories, because
   scanning `__init__.py` files cannot fail for a file that does not exist).
5. **Providers that are not shipped publicly keep the same token**, in their own distribution:
   `providers/qoder.py` in core became `engrix_router_providers/qoder/`. Discovery does not care
   which repository a provider module lives in, so the naming rule is the only contract.

## What this costs when it is ignored

The stale-reference guard now treats `openai_sse`, `anthropic_messages`, `openai_compatible`,
`api/compat` and `api.compat` as errors, so a leftover import fails CI instead of quietly pointing
at a file that no longer exists. History lives here and in ADR-0003's map, which are the only
places allowed to name the old paths.

## Rename executed (2026-10-09)

| old | new | references updated |
|---|---|---|
| `core/formats/openai_sse.py` | `core/formats/openai.py` | 12 in 6 files |
| `core/formats/anthropic_messages.py` | `core/formats/anthropic.py` | 7 in 5 files |
| `api/compat.py` | `api/openai.py` | 5 in 4 files |
| `providers/openai_compatible.py` | `providers/openai.py` | 16 in 6 files |
| `tests/contract/test_openai_compatible.py` | `tests/contract/test_openai_provider.py` | — |

Import aliases that carried the old word (`sse_mod`) became `openai_fmt`, and the Anthropic route
imports its format module as `anthropic_fmt` / `fmt` so a reader never wonders whether `anthropic`
inside `api/anthropic.py` means the route or the format.
