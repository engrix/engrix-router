# ADR-0001 — Canonical request/response format

- Status: Accepted
- Date: 2026-10-09
- Lives in: `engrix_router/core/types/` (foundation layer; imports nothing above it)
- Evidence: live measurements recorded in `knowledge/data/DATA_QODER_WIRE_2026-10-08.md`
  and `data/live_qoder_check.json` (private), plus the consumer contract in
  `engrix-agent/agent/streaming.py` and `key_pool.py`.

## Decision

One canonical shape in the middle. Inbound adapters (OpenAI, Anthropic) decode into it;
each provider encodes it into the vendor's native wire and decodes the vendor's stream
back into canonical events. `2 + N`, never `2 × N`.

The canonical shape **follows OpenAI's chat-completion vocabulary**, deliberately: the
primary client uses `openai-python`, and OpenAI's field names are the lingua franca both
SDK ecosystems can be mapped to. We are not inventing a new protocol; we are pinning a
contract and adding the fields OpenAI's shape cannot express.

## Types

```python
# core/types/request.py
@dataclass
class TextBlock:      text: str
@dataclass
class ImageRef:       url: str | None = None; media_type: str | None = None; data_b64: str | None = None

@dataclass
class Message:
    role: Literal["system", "user", "assistant", "tool"]
    content: list[TextBlock | ImageRef] | None      # str is accepted and normalised
    tool_calls: list[ToolCall] = ()                 # assistant
    tool_call_id: str | None = None                 # tool

@dataclass
class ToolCall:
    id: str; name: str; arguments: dict[str, Any]   # arguments parsed, not a string

@dataclass
class Request:
    model_ref: str                                  # raw "qd/qfmodel"; resolved later
    messages: list[Message]
    tools: list[Tool] = ()
    tool_choice: str | dict | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = None            # canonical name for max_tokens / max_completion_tokens
    stop: list[str] = ()
    reasoning_effort: str | None = None             # "minimal".."max"; provider maps to its own efforts
    stream: bool = False
    vendor_options: dict[str, dict] = field(default_factory=dict)   # {"qoder": {...}}
```

```python
# core/types/events.py — the ONLY streaming contract. SSE framing is an api/ concern.
@dataclass
class DeltaText:      text: str
@dataclass
class DeltaReasoning: text: str
@dataclass
class DeltaToolCall:  index: int; id: str | None = None; name: str | None = None; arguments: str | None = None
@dataclass
class Finish:         reason: str                    # stop | length | tool_calls | content_filter
@dataclass
class UsageEvent:     usage: Usage
@dataclass
class ErrorEvent:     error: Error
```

```python
# core/types/usage.py
@dataclass
class Usage:
    prompt: int = 0; completion: int = 0; reasoning: int = 0
    cached: int = 0; cache_creation: int = 0; total: int = 0
    billable: bool | None = None                     # vendor said so; None = unknown
    credits: float | None = None                     # vendor's own unit, NOT usd
    raw: dict[str, Any] = field(default_factory=dict)  # untouched vendor block, for audit
```

```python
# core/types/errors.py
@dataclass
class Error:
    kind: str            # canonical class: quota_daily | pricing_blocked | rate_limited | ...
    message: str         # English, client-visible (ADR-0000)
    client_status: int   # OUR decision, see the consumer contract below
    vendor_code: str | None = None
    retry_after_s: int | None = None
    reset_at: datetime | None = None
```

## Why each field is there (measured, not guessed)

| field | evidence |
|---|---|
| `DeltaReasoning` separate from `DeltaText` | Qoder streams `delta.reasoning_content` alongside `delta.content` (live run: reasoning 42 tokens, content `391`). Anthropic does the same with thinking blocks. Merging them corrupts the client's answer text and its token accounting. |
| `DeltaToolCall.index` is the merge key | Live measurement: first frame carries `id` + `name` with empty `arguments`; later frames carry `id: ""` and streamed argument fragments, `index` stays stable. `engrix-agent/agent/streaming.py:296-314` accumulates by index, so canonical must keep it. |
| `Usage.cached` / `cache_creation` | Anthropic bills them separately (`cache_read_input_tokens`, `cache_creation_input_tokens`); a single `prompt` number would be a lie. |
| `Usage.billable` / `credits` | Qoder's usage frame carries `billable:false`, `credits`, `original_credits`. These are the only numbers that answer "did this call actually cost the account" — the reason this repo exists. Keep the vendor unit; never inflate into USD. |
| `Usage.raw` | The ledger columns exist but nothing fills them today. Raw survives so a vendor change is visible in the trace instead of silently dropped. |
| `UsageEvent` may arrive **after** `Finish` | Measured: Qoder sends usage in the frame after `finish_reason`, with `choices: []`. A stream reader that stops at `finish` loses usage. |
| `Error.kind` + `client_status` split | The caller treats 400/401/403 as fatal and benches our key for 24h (`streaming.py:459-464`, `key_pool.py:67-85`). So upstream quota/health problems MUST surface as 503/429 + `Retry-After`; 401/403 are reserved for "you are not authenticated to this gateway". This mapping lives in one place, not per provider. |
| `model_ref` unresolved until routing | Canonical must not assume the vendor's model id equals the requested one; vendors that alias (Qoder echoes `"auto"`, which is useless for audit) resolve to the real key inside the provider. |
| `vendor_options` namespaced | Escape hatch so a vendor-specific knob does not pollute `core/types`. Rule: core never reads a specific namespace, and nothing in the pipeline may *require* it. |

## Rules that fall out of this

1. **No vendor names or vendor fields in `core/types`.** If `if provider ==` appears
   here, the canonical shape is wrong — that is the acceptance test for this ADR.
2. **Canonical events, not strings.** Providers emit typed events; `api/openai/stream.py`
   and `api/anthropic/stream.py` each render their own SSE. `stream=false` is assembled
   from the same event stream (one path, already validated end-to-end on a live vendor).
3. **Unsupported parameters are reported, not silently dropped.** The response/trace
   records `ignored: ["top_p"]` when a vendor cannot honour it. (Silent reinterpretation
   is the 9router failure mode this repo replaces.)
4. **Hooks may reject; providers may transform; only the pipeline sequences.** No hook
   mutates `messages`/`system`/`tools` (ADR-0000) — asserted in `tests/contract/`.
5. **Non-stream and stream share accounting.** Usage is merged field-wise max across the
   stream (vendors send partial usage in several frames), never summed naively, never
   padded (the `+2000` buffer we came from inflates every ledger line).

## Alternatives considered

- *Vendor passthrough (dict of dicts, no types)* — what 9router effectively does; fast to
  write, impossible to test across providers, and hides the silent-reinterpretation bug
  class. Rejected.
- *Our own neutral protocol (new field names)* — cleaner on paper, but every client and
  every SDK in the house would need a translation layer, and the primary client speaks
  OpenAI. Rejected for pragmatic reasons; documented here so the tradeoff is on record.

## Open questions before implementation

1. Does `Request` need a `metadata`/`user` field carried through to logs for per-user
   budgeting, or is the API key sufficient as identity? (current: API key + lane)
2. Multi-modal inbound: `ImageRef` supports url/base64, but only Qoder's upload path and
   OpenAI's url form are measured so far — Anthropic's base64 shape is assumed. Verify in
   the Anthropic stage before trusting it.

## Implementation note (2026-10-09, after the Anthropic stage)

What shipped, and where it differs from the sketch above:

* **Canonical frames are still OpenAI-shaped dicts, not the `DeltaText`/`UsageEvent`
  dataclasses sketched here.** What this ADR actually demanded is enforced and tested: no
  vendor names in `core`, one pipeline for both `stream=true` and `stream=false`, and each
  inbound format rendering its own bytes. `pipeline.runner.stream_frames()` is the single
  frame source; `api/openai.py` renders OpenAI SSE and `api/anthropic.py` renders
  Anthropic events (`message_start` → `content_block_*` → `message_delta` → `message_stop`,
  failures as `event: error`, no `[DONE]`). Swapping frames for typed event objects is now
  a refactor of *representation* with no new invariant to gain, so it is deliberately not
  done here.
* **Open question 2 is still open.** Anthropic inbound passes an unknown content block
  through untouched; the base64-image → data-URL mapping is written nowhere and never
  measured. Do not trust image traffic on that path.
* **`Usage.billable` / `credits` are no longer a plan.** `subscribers.usage.vendor_signals()`
  reads `billable` / `credits` / `original_credits` out of any vendor usage block,
  `RequestTrace.attach_usage(..., signals=...)` keeps them, and `trace.finish()` writes
  `requests.billable`, `requests.credits`, `requests.credits_original` (schema v2 +
  `MIGRATIONS`). `billable` stays NULL when the vendor never said: "unknown" and "false"
  are different answers and the burn-rate report depends on the difference.
* **Rule 3 ("unsupported parameters are reported") is NOT implemented.** Nothing collects
  an `ignored: [...]` list today. The safer half of the rule holds — an unknown field is
  passed through untouched rather than dropped or reinterpreted — but "reported" needs a
  decision about where the notice belongs (response header? trace field? dashboard badge?)
  and is left open rather than guessed at.
* **`total` is re-clamped after merging.** A field-wise max over frames is right for split
  usage, but a per-frame `total` computed from partial information (Anthropic sends input
  in `message_start`, output in `message_delta`) can end up lower than the sum, so
  `merge()` re-derives `total = max(total, prompt + completion)`.
