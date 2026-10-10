// View Requests & Trace (TASK-43): daftar request + panel trace satu request.
// Disiplin data: kolom selalu berlabel; stage payload dirender terstruktur
// (step · nama · arah · byte) dengan potongan >4000 B ditandai eksplisit,
// bukan digantung sebagai teks mentah.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { t } from "./i18n";
import { fmt } from "./i18n";
import { clock, money, num, short } from "./format";
import { Badge, Card, EmptyState, InlineMsg } from "./components";
import type { RequestRow, TraceItem } from "./types";

export function RequestsView({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<RequestRow[]>([]);
  const [provider, setProvider] = useState("");
  const [status, setStatus] = useState("");
  const [limit, setLimit] = useState("30");
  const [err, setErr] = useState<string | null>(null);
  const [trace, setTrace] = useState<TraceItem | null>(null);
  const [traceErr, setTraceErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    const q = new URLSearchParams({ limit: limit || "30" });
    if (provider) q.set("provider", provider);
    if (status) q.set("status", status);
    try {
      setRows(await api<RequestRow[]>("/api/usage/requests?" + q.toString()));
      setErr(null);
    } catch (e) { setErr((e as ApiError).message); }
  }, [provider, status, limit]);

  useEffect(() => { load(); }, [load, refreshKey]);

  async function openTrace(id: string) {
    setTraceErr(null);
    try {
      setTrace(await api<TraceItem>(`/api/usage/requests/${id}`));
    } catch (e) { setTraceErr((e as ApiError).message); }
  }

  return (
    <div>
      <Card
        title={t("req.title")}
        tools={
          <>
            <input className="tiny-input" placeholder={t("req.provider_ph")} value={provider} onChange={e => setProvider(e.target.value)} size={12} />
            <input className="tiny-input" placeholder={t("req.status_ph")} value={status} onChange={e => setStatus(e.target.value)} size={14} />
            <input className="tiny-input" type="number" value={limit} onChange={e => setLimit(e.target.value)} size={4} title={t("req.limit")} />
            <button className="tight" onClick={load}>{t("req.load")}</button>
          </>
        }
      >
        <InlineMsg msg={err} kind="err" />
        <table>
          <thead><tr>
            <th>{t("common.time")}</th><th>{t("common.provider_model")}</th><th>{t("common.status")}</th>
            <th>{t("common.col_http")}</th><th>{t("req.col_ttft")}</th><th>{t("req.col_total")}</th><th>{t("common.token")}</th><th>{t("common.cost")}</th><th>{t("common.account")}</th>
          </tr></thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.id} className="clickable" onClick={() => openTrace(r.id)}>
                <td className="tiny">{clock(r.ts)}</td>
                <td className="mono tiny">{r.provider}/{r.model}</td>
                <td><Badge status={r.status} /></td>
                <td className="tiny">{num(r.http_out)}</td>
                <td className="tiny">{num(r.ttft_ms)} ms</td>
                <td className="tiny">{num(r.total_ms)} ms</td>
                <td className="tiny">{num(r.total)}</td>
                <td className="tiny">{money(r.cost_usd)}</td>
                <td className="mono tiny" title={r.connection_id}>{short(r.connection_id)}</td>
              </tr>
            ))}
            {!rows.length && <EmptyState label={t("req.empty")} colSpan={9} />}
          </tbody>
        </table>
        <p className="tiny muted">{t("req.row_hint")}</p>
      </Card>

      {traceErr && <InlineMsg msg={traceErr} kind="err" />}

      {trace && (
        <Card title={fmt("trace.title", { value: short(trace.id, 12) })}>
          <div className="row tiny muted trace-head">
            <Badge status={trace.status} />
            <span>{t("trace.http_out")} {num(trace.http_out)}</span>
            <span>{t("trace.upstream_status")} {trace.upstream_status === null ? "—" : num(trace.upstream_status)}</span>
            <span>{t("req.col_ttft")} {num(trace.ttft_ms)} ms</span>
            <span>{t("req.col_total")} {num(trace.total_ms)} ms</span>
            <span>{t("trace.frames")} {num(trace.frames)}</span>
            <span>{t("trace.usage_source")} {trace.usage_source || "—"}</span>
            <span className="mono">{trace.provider}/{trace.model}</span>
            <span>{t("trace.prompt")} {num(trace.prompt)}</span>
            <span>{t("trace.completion")} {num(trace.completion)}</span>
            <span>{t("trace.reasoning")} {num(trace.reasoning)}</span>
            <span>{t("trace.cached")} {num(trace.cached)}</span>
          </div>
          {trace.error_text && <pre className="code err-text">{trace.error_text}</pre>}
          {(trace.stages || []).length
            ? trace.stages.map((s, i) => {
              let body = s.payload_json || "";
              const trunc = body.length > 4000;
              if (trunc) body = body.slice(0, 4000);
              return (
                <div className="stage" key={i}>
                  <div className="who">
                    {s.step} · {s.name} · {s.direction} · {num(s.bytes)} B
                    {trunc && s.truncated ? ` · ${fmt("trace.truncated", { value: num(s.original_bytes) })}` : ""}
                  </div>
                  <pre className="code">{body}</pre>
                </div>
              );
            })
            : <p className="muted tiny">{t("trace.no_stages")}</p>}
        </Card>
      )}
    </div>
  );
}
