// View Overview (TASK-43): ringkasan satu layar dengan KARTU TERPISAH per
// bagian -- KPI, quota tracker semua vendor, health akun, budget, drift,
// penggunaan per provider/model, dan quick-start klien. Angka yang sama dengan
// tab Providers karena keduanya baca snapshot /api/quota yang sama.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError, quotaSnapshot, token } from "./api";
import { clientBase, fmt, t } from "./i18n";
import { clock, money, num, short } from "./format";
import { Badge, Card, EmptyState, InlineMsg, QuotaRow, Tile } from "./components";
import type { DayChart, State, Stats, QuotaSnapshot } from "./types";

export function OverviewView({ refreshKey }: { refreshKey: number }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [state, setState] = useState<State | null>(null);
  const [quota, setQuota] = useState<QuotaSnapshot | null>(null);
  const [chart, setChart] = useState<DayChart[]>([]);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [s, st, q, ch] = await Promise.all([
        api<Stats>("/api/usage/stats?period=today"),
        api<State>("/api/state"),
        quotaSnapshot(),
        api<DayChart[]>("/api/usage/chart?period=7d").catch(() => []),
      ]);
      setStats(s); setState(st); setQuota(q); setChart(ch || []);
      setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }, []);

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [refreshKey]);

  async function clearDrift() {
    try {
      await api("/api/drift/clear", { method: "POST" });
      setErr(null);
      load();
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }

  const totals = stats?.totals;
  const worst = quota?.worst;

  return (
    <div>
      <InlineMsg msg={err} kind="err" />

      {totals && (
        <div className="tiles">
          <Tile
            label={t("overview.req_today")}
            value={<>{num(totals.requests)} <small>{t("common.req")}</small></>}
            note={fmt("overview.req_split", { ok: num(totals.ok), errors: num(totals.errors) })}
          />
          <Tile
            label={t("overview.tokens_today")}
            value={<>{num(totals.tokens)} <small>{t("common.token")}</small></>}
            note={fmt("overview.token_split", { prompt: num(totals.prompt), cached: num(totals.cached) })}
          />
          <Tile
            label={t("overview.cost")}
            value={money(totals.cost)}
            note={t("overview.cost_note")}
          />
          {state && (
            <Tile
              label={t("nav.providers")}
              value={<>{state.providers.length} <small>{t("overview.vendors_unit")}</small></>}
              note={<span>{state.providers.map(p => <span key={p.id} className="badge violet" style={{ margin: 2 }} title={p.is_node ? t("providers.node_note") : t("providers.vendor_note")}>{p.id}</span>)}</span>}
            />
          )}
          {state && (
            <Tile
              label={t("overview.accounts_tile")}
              value={<>{state.health.length} <small>{t("overview.accounts_unit")}</small></>}
              note={t("overview.accounts_note")}
            />
          )}
        </div>
      )}

      <Card title={t("quota.title")} tools={quota && !quota.error ? <button className="tiny" onClick={() => quotaSnapshot(true).then(setQuota)}>{t("conn.refresh_quota")}</button> : null}>
        {quota?.error
          ? <p className="tiny err-text">{quota.error}</p>
          : (quota?.providers || []).filter(p => p.available && (p.readings || []).length).length === 0
            ? ((quota?.providers || []).length === 0
              ? <EmptyState label={t("quota.empty")} />
              /* Cache dingin / vendor tidak melaporkan: tampilkan ALASAN per
                 koneksi, bukan klaim kosong yang kontradiktif dengan baris
                 "most critical" di bawahnya (TASK-44). */
              : (quota?.providers || []).map(p => (
                <div className="qblock" key={p.connection_id}>
                  <div className="qhead">
                    <span className="mono">{p.provider}</span>
                    <span className="tiny muted">{p.name || short(p.connection_id)}</span>
                  </div>
                  <p className="tiny muted">{p.reason || t("providers.no_quota")}</p>
                </div>
              )))
            : (quota?.providers || []).filter(p => p.available && (p.readings || []).length).map(p => (
              <div className="qblock" key={p.connection_id}>
                <div className="qhead">
                  <span className="mono">{p.provider}</span>
                  <span className="tiny muted">{p.name || short(p.connection_id)}</span>
                  <span className="tiny muted">{fmt("quota.fetched", { value: clock(p.fetched_at) })}</span>
                </div>
                {p.readings.map((r, i) => <QuotaRow key={r.scope + i} reading={r} />)}
              </div>
            ))}
        {worst && (quota?.providers || []).some(p => p.available && (p.readings || []).length) && (
          <p className="tiny muted">
            {fmt("quota.worst", {
              provider: worst.provider,
              scope: worst.scope,
              remaining: num(worst.remaining),
              account: (quota?.providers || []).find(p => p.connection_id === worst.connection_id)?.name || short(worst.connection_id),
              pct: worst.remaining_pct + "%",
            })}
          </p>
        )}
      </Card>

      {chart.length > 0 && (
        <Card title={t("overview.chart7")}>
          <Spark data={chart} />
          <div className="row tight tiny muted">
            <span><i className="dot ok" /> {t("overview.chart_requests")}</span>
            <span><i className="dot mid" /> {t("overview.chart_tokens")}</span>
          </div>
        </Card>
      )}

      {state && (
        <Card title={t("overview.health")}>
          <table>
            <thead><tr>
              <th>{t("common.account")}</th><th>{t("common.provider")}</th><th>{t("common.status")}</th><th>{t("common.error")}</th><th>{t("overview.locks")}</th><th>{t("common.last_used")}</th>
            </tr></thead>
            <tbody>
              {state.health.map(h => (
                <tr key={(h.provider || "?") + ":" + h.connection_id}>
                  <td className="mono tiny"><span title={h.connection_id}>{h.name || short(h.connection_id)}</span></td>
                  <td className="mono tiny">{h.provider || "—"}</td>
                  <td><Badge status={h.test_status} /></td>
                  <td className="tiny">{h.error_class || "—"}</td>
                  <td className="tiny">{(h.locks || []).map(l => `${l.model} → ${clock(l.locked_until)}`).join(" · ") || "—"}</td>
                  <td className="tiny">{h.last_used_at ? clock(h.last_used_at) : "—"}</td>
                </tr>
              ))}
              {!state.health.length && <EmptyState label={t("common.no_connections")} colSpan={6} />}
            </tbody>
          </table>
        </Card>
      )}

      {stats?.budget?.rows && (
        <Card title={t("overview.budget")}>
          <table>
            <thead><tr><th>{t("common.scope")}</th><th>{t("overview.budget_used")}</th><th>{t("overview.budget_pct")}</th></tr></thead>
            <tbody>
              {stats.budget.rows.map(r => (
                <tr key={r.scope}>
                  <td className="mono tiny" title={r.scope}>
                    {r.scope.startsWith("key:")
                      ? t("overview.budget_key_scope") + " " + r.scope.slice(4, 12) + "…"
                      : r.scope}
                  </td>
                  <td className="num">{num(r.used_tokens)} / {r.token_limit ? num(r.token_limit) : "∞"}</td>
                  <td className="num">{r.used_pct === null ? "—" : r.used_pct + "%"}</td>
                </tr>
              ))}
              {!stats.budget.rows.length && <EmptyState label={t("overview.budget_empty")} colSpan={3} />}
            </tbody>
          </table>
        </Card>
      )}

      {state?.drift && (
        <Card title={t("overview.drift")} tone={state.drift.tripped ? "danger" : undefined}>
          <div className="row tight">
            <Badge status={state.drift.tripped ? "unavailable" : "active"} />
            <span className="tiny muted">{state.drift.reason || t("overview.no_trip")}</span>
            {state.drift.tripped && (
              <button className="tiny danger" onClick={clearDrift} disabled={!token()}>{t("overview.unfreeze")}</button>
            )}
          </div>
        </Card>
      )}

      {stats && (
        <>
          <Card title={t("overview.usage_provider")}>
            <table>
              <thead><tr><th>{t("common.provider")}</th><th>{t("common.req")}</th><th>{t("common.token")}</th><th>{t("common.cost")}</th><th>{t("common.failed")}</th></tr></thead>
              <tbody>
                {(stats.by_provider || []).map(r => (
                  <tr key={r.provider}>
                    <td className="mono">{r.provider}</td>
                    <td>{num(r.requests)}</td><td>{num(r.tokens)}</td><td>{money(r.cost)}</td><td>{num(r.errors ?? 0)}</td>
                  </tr>
                ))}
                {!(stats.by_provider || []).length && <EmptyState label={t("overview.no_traffic")} colSpan={5} />}
              </tbody>
            </table>
          </Card>
          <Card title={t("overview.usage_model")}>
            <table>
              <thead><tr><th>{t("common.provider")}</th><th>{t("common.model")}</th><th>{t("common.req")}</th><th>{t("common.token")}</th><th>{t("common.cost")}</th></tr></thead>
              <tbody>
                {(stats.by_model || []).map(r => (
                  <tr key={r.provider + "/" + r.model}>
                    <td className="mono tiny">{r.provider}</td>
                    <td className="mono tiny">{r.model}</td>
                    <td>{num(r.requests)}</td><td>{num(r.tokens)}</td><td>{money(r.cost)}</td>
                  </tr>
                ))}
                {!(stats.by_model || []).length && <EmptyState label={t("overview.no_traffic")} colSpan={5} />}
              </tbody>
            </table>
          </Card>
        </>
      )}

      <Card title={t("overview.client_title")}>
        <details>
          <summary className="tiny muted">{t("overview.client_note")}</summary>
          <pre className="code" style={{ marginTop: 8 }}>
{`POST ${clientBase()}/v1/chat/completions
authorization: Bearer <client key -- create one in the API keys tab>
{"model": "openai/gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}]}`}
          </pre>
        </details>
      </Card>
    </div>
  );
}

// Sparkline SVG murni (tanpa dependency chart): dua seri requests+tokens per
// hari dengan label tanggal eksplisit di bawah -- bukan angka terbang.
function Spark(props: { data: DayChart[] }) {
  const d = props.data;
  const w = 480, h = 64, pad = 4;
  const maxReq = Math.max(1, ...d.map(x => x.requests));
  const maxTok = Math.max(1, ...d.map(x => x.tokens));
  const pts = (get: (x: DayChart) => number, max: number) =>
    d.map((x, i) => `${pad + (i * (w - 2 * pad)) / Math.max(1, d.length - 1)},${h - pad - (get(x) / max) * (h - 2 * pad)}`).join(" ");
  // Satu hari data = satu titik; polyline butuh 2 titik supaya tidak hilang.
  const one = d.length === 1;
  const line = (get: (x: DayChart) => number, max: number) =>
    one ? `${pts(get, max)} ${pts(get, max)}` : pts(get, max);
  return (
    <div>
      <svg viewBox={`0 0 ${w} ${h}`} style={{ width: "100%", height: "auto" }} role="img" aria-label={t("overview.chart")}>
        <polyline fill="none" stroke="var(--ok)" strokeWidth="1.5" points={line(x => x.requests, maxReq)} />
        <polyline fill="none" stroke="var(--warn)" strokeWidth="1.5" points={line(x => x.tokens, maxTok)} />
        {one && (
          <>
            <circle cx={pad} cy={h - pad - (d[0].requests / maxReq) * (h - 2 * pad)} r="3" fill="var(--ok)" />
            <circle cx={pad} cy={h - pad - (d[0].tokens / maxTok) * (h - 2 * pad)} r="3" fill="var(--warn)" />
          </>
        )}
      </svg>
      <div className="row tiny muted" style={{ justifyContent: "space-between" }}>
        <span>{d[0]?.label}</span>
        <span>{t("overview.chart_scale")}: {t("overview.chart_requests")} max {num(maxReq)}, {t("overview.chart_tokens")} max {num(maxTok)}</span>
        <span>{d[d.length - 1]?.label}</span>
      </div>
    </div>
  );
}
