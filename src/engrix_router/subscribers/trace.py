"""Request trace recorder: one `requests` row + the `request_stages` children.

Replaces two 9router mechanisms at once:
  * usageHistory, which is written only on success (handlers/chatCore.js:400,475) --
    which is why the owner's DB has 35,453 rows and every single one is status 'ok', and
  * the 7-file dump per request under logs/translator/ (utils/requestLogger.js:17),
    unrotated, with header masking commented out.

Here it is one row per request, success OR failure, plus pipeline stages as child rows
that can be fetched per request_id. Payloads are cut with explicit `truncated` +
`original_bytes` markers -- following the good `_truncated/_originalSize/_preview`
pattern from requestDetailsRepo.js:80-86 instead of silently substringing.
"""
from __future__ import annotations

import json
from typing import Any

from engrix_router.storage.sqlite import execute, now_ms, query, query_one, transaction
from engrix_router.storage import settings
from engrix_router.core.ids import new_id

STATUS_IN_FLIGHT = "in_flight"
STATUS_OK = "ok"
STATUS_CLIENT_ERROR = "client_error"
STATUS_UPSTREAM_ERROR = "upstream_error"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_ABORTED = "aborted"
STATUS_LOCKED = "locked"
STATUS_BUDGET = "budget_exceeded"
STATUS_DRY_RUN = "dry_run"

STEP_CLIENT_IN = 1
STEP_OPENAI_MID = 2
STEP_PROVIDER_OUT = 3
STEP_UPSTREAM_IN = 4
STEP_CLIENT_OUT = 5
STEP_DRY_RUN = 6

_STEP_NAMES = {
    STEP_CLIENT_IN: "client_in",
    STEP_OPENAI_MID: "openai_mid",
    STEP_PROVIDER_OUT: "provider_out",
    STEP_UPSTREAM_IN: "upstream_in",
    STEP_CLIENT_OUT: "client_out",
    STEP_DRY_RUN: "dry_run",
}


class RequestTrace:
    """One request, one object. The pipeline holds it; the API never writes traces directly."""

    def __init__(self, request_id: str, *, endpoint: str, kind: str = "chat",
                 api_key_id: str | None = None, budget_lane: str = "interactive") -> None:
        self.request_id = request_id
        self.started_ms = now_ms()
        self.endpoint = endpoint
        self.kind = kind
        self.api_key_id = api_key_id
        self.budget_lane = budget_lane
        self.provider: str | None = None
        self.model: str | None = None
        self.node_id: str | None = None
        self.connection_id: str | None = None
        self.proxy_pool_id: str | None = None
        self.tier: str | None = None
        self.ttft_ms: int | None = None
        self.frames = 0
        self.usage: dict[str, int] = {}
        self.usage_source = "upstream"
        self.billable: bool | None = None
        self.credits: float | None = None
        self.credits_original: float | None = None
        self.upstream_status: int | None = None
        self.error_class: str | None = None
        self.error_code: str | None = None
        self.error_text: str | None = None
        self.http_out: int | None = None
        self.status = STATUS_IN_FLIGHT
        self.dry_run = False
        self._client_seen_chars = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def record(self) -> None:
        execute(
            "INSERT INTO requests(id, ts, api_key_id, provider, model, connection_id, node_id, endpoint,"
            " kind, status, budget_lane, proxy_pool_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.request_id, self.started_ms, self.api_key_id, self.provider, self.model,
             self.connection_id, self.node_id, self.endpoint, self.kind, STATUS_IN_FLIGHT,
             self.budget_lane, self.proxy_pool_id),
        )

    def bind_target(self, *, provider: str, model: str, node_id: str | None = None) -> None:
        self.provider, self.model, self.node_id = provider, model, node_id
        execute("UPDATE requests SET provider=?, model=?, node_id=? WHERE id=?",
                (provider, model, node_id, self.request_id))

    def bind_connection(self, connection_id: str, *, proxy_pool_id: str | None = None) -> None:
        self.connection_id = connection_id
        self.proxy_pool_id = proxy_pool_id
        execute("UPDATE requests SET connection_id=?, proxy_pool_id=? WHERE id=?",
                (connection_id, proxy_pool_id, self.request_id))

    # ── stages ───────────────────────────────────────────────────────────────
    def stage(self, step: int, payload: Any, *, direction: str, model_hint: str | None = None) -> None:
        if not settings.get_bool("observability.enabled"):
            return
        if isinstance(payload, (bytes, bytearray)):
            text = payload.decode("utf-8", "replace")
        elif isinstance(payload, str):
            text = payload
        else:
            try:
                text = json.dumps(payload, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(payload)
        limit = settings.get_int("observability.max_stage_bytes")
        size = len(text.encode("utf-8", "replace"))
        truncated = 1 if size > limit else 0
        stored = text[:limit] if truncated else text
        execute(
            "INSERT INTO request_stages(request_id, step, name, direction, payload_json, bytes,"
            " truncated, original_bytes, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (self.request_id, step, _STEP_NAMES.get(step, f"step{step}"), direction, stored,
             min(size, limit), truncated, size if truncated else None, now_ms()),
        )

    def note_first_token(self) -> None:
        if self.ttft_ms is None:
            self.ttft_ms = now_ms() - self.started_ms

    def note_frame(self, client_text_chars: int = 0) -> None:
        self.frames += 1
        self._client_seen_chars += max(0, int(client_text_chars))

    @property
    def seen_content_chars(self) -> int:
        """Content characters already delivered to the client -- the basis of the
        completion estimate when the upstream sends no usage at all."""
        return self._client_seen_chars

    def attach_usage(self, usage: dict[str, Any] | None,
                     *, signals: dict[str, Any] | None = None) -> None:
        """Store the final canonical usage so finish() never has to guess."""
        from engrix_router.subscribers import usage as usage_mod

        if usage_mod.has_any(usage):
            self.usage = usage_mod.canonicalize(usage)
        for key in ("billable", "credits", "credits_original"):
            if signals and signals.get(key) is not None:
                setattr(self, key, signals[key])

    # ── finalize ─────────────────────────────────────────────────────────────
    def finish(self, *, status: str, http_out: int, usage: dict[str, Any] | None = None,
               usage_source: str | None = None, error_class: str | None = None,
               error_code: str | None = None, error_text: str | None = None,
               upstream_status: int | None = None, billable: bool | None = None,
               cost_usd: float | None = None) -> dict[str, Any]:
        from engrix_router.subscribers import usage as usage_mod

        self.status = status
        self.http_out = http_out
        if billable is not None:
            self.billable = bool(billable)
        if usage is not None:
            self.usage = usage_mod.canonicalize(usage)
        if usage_source:
            self.usage_source = usage_source
        self.error_class = error_class
        self.error_code = error_code
        self.error_text = (error_text or "")[:900] or None
        self.upstream_status = upstream_status
        total_ms = now_ms() - self.started_ms
        provider = self.provider or ""
        model = self.model or ""
        if cost_usd is None:
            from engrix_router.subscribers import pricing

            cost_usd, _known = pricing.cost_for(provider, model, self.usage)
        execute(
            "UPDATE requests SET finished_at=?, status=?, http_out=?, upstream_status=?, error_class=?,"
            " error_code=?, error_text=?, ttft_ms=?, total_ms=?, frames=?, prompt=?, completion=?,"
            " reasoning=?, cached=?, cache_creation=?, total=?, usage_source=?, billable=?, credits=?,"
            " credits_original=?, cost_usd=?, tier=? WHERE id=?",
            (now_ms(), status, http_out, upstream_status, error_class, error_code, self.error_text,
             self.ttft_ms, total_ms, self.frames, self.usage.get("prompt", 0),
             self.usage.get("completion", 0), self.usage.get("reasoning", 0), self.usage.get("cached", 0),
             self.usage.get("cache_creation", 0), self.usage.get("total", 0), self.usage_source,
             None if self.billable is None else int(self.billable), self.credits,
             self.credits_original, cost_usd, self.tier, self.request_id),
        )
        self._rollup(status, cost_usd)
        self._trim()
        return {"request_id": self.request_id, "status": status, "http_out": http_out,
                "total_ms": total_ms, "usage": self.usage, "cost_usd": cost_usd,
                "usage_source": self.usage_source}

    def _rollup(self, status: str, cost_usd: float) -> None:
        """Daily rollup in the SAME transaction as the request row update -- the good
        pattern from usageRepo.js:257-306 (usage + daily + lifetime atomic)."""
        if not self.provider:
            return
        from engrix_router.storage.sqlite import date_key_utc

        ok = 1 if status == STATUS_OK else 0
        error = 1 if status in (STATUS_UPSTREAM_ERROR, STATUS_RATE_LIMITED, STATUS_CLIENT_ERROR) else 0
        with transaction() as db:
            db.execute(
                "INSERT INTO usage_daily(date_key, provider, model, requests, ok, errors, prompt,"
                " completion, cached, cost_usd, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(date_key, provider, model) DO UPDATE SET"
                " requests = usage_daily.requests + 1,"
                " ok = usage_daily.ok + excluded.ok,"
                " errors = usage_daily.errors + excluded.errors,"
                " prompt = usage_daily.prompt + excluded.prompt,"
                " completion = usage_daily.completion + excluded.completion,"
                " cached = usage_daily.cached + excluded.cached,"
                " cost_usd = usage_daily.cost_usd + excluded.cost_usd,"
                " updated_at = excluded.updated_at",
                (date_key_utc(), self.provider, self.model or "?", 1, ok, error,
                 self.usage.get("prompt", 0), self.usage.get("completion", 0),
                 self.usage.get("cached", 0), float(cost_usd or 0.0), now_ms()),
            )

    def _trim(self) -> None:
        """Ring cap + retention (9router caps requestDetails at 1000 rows,
        requestDetailsRepo.js:127-133, and gives usageHistory no retention at all)."""
        max_records = settings.get_int("observability.max_records")
        execute(
            "DELETE FROM request_stages WHERE request_id IN ("
            " SELECT id FROM requests ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (max_records,),
        )
        execute(
            "DELETE FROM requests WHERE id IN ("
            " SELECT id FROM requests ORDER BY ts DESC LIMIT -1 OFFSET ?)",
            (max_records,),
        )
        days = settings.get_int("observability.retention_days")
        if days > 0:
            execute(
                "DELETE FROM requests WHERE ts < (strftime('%s','now') * 1000) - ? * 86400000"
                " AND status != ?",
                (days, STATUS_IN_FLIGHT),
            )


def begin(endpoint: str, *, kind: str = "chat", api_key_id: str | None = None,
          budget_lane: str = "interactive", request_id: str | None = None) -> RequestTrace:
    trace = RequestTrace(request_id or new_id(), endpoint=endpoint, kind=kind,
                         api_key_id=api_key_id, budget_lane=budget_lane)
    trace.record()
    return trace


def get(request_id: str) -> dict[str, Any] | None:
    row = query_one("SELECT * FROM requests WHERE id = ?", (request_id,))
    if row is None:
        return None
    item = dict(row)
    item["stages"] = [
        dict(stage)
        for stage in query(
            "SELECT step, name, direction, payload_json, bytes, truncated, original_bytes"
            " FROM request_stages WHERE request_id = ? ORDER BY step, id",
            (request_id,),
        )
    ]
    return item


def recent(limit: int = 50, *, provider: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, ts, provider, model, connection_id, status, http_out, error_class, ttft_ms, total_ms, total, cost_usd FROM requests"
    clauses: list[str] = []
    params: list[Any] = []
    if provider:
        clauses.append("provider = ?")
        params.append(provider)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return [dict(row) for row in query(sql, params)]
