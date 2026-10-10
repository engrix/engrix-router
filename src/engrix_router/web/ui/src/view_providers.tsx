// View Providers (TASK-43): SATU kartu per vendor. Di dalam kartu: meta vendor,
// tombol OAuth+ (start + poll sampai vendor menjawab), lalu per akun: nama,
// badge health, antrian prioritas, proxy pool, aksi Test/Catalog/Test models/
// Edit/Delete, dan blok kuota akun itu sendiri. Tidak ada tabel mentah:
// semua nilai lewat label katalog + formatter.

import { useEffect, useRef, useState } from "react";
import { api, ApiError, quotaSnapshot, token } from "./api";
import { fmt, t } from "./i18n";
import { dcount, num, short } from "./format";
import {
  Badge, Card, ConfirmBar, EmptyState, InlineMsg, Meta, QuotaGroup,
} from "./components";
import type {
  CatalogRow, Connection, Health, ModelTestRow, PoolRow, ProviderDef, QuotaSnapshot,
} from "./types";

type Panel = { kind: "catalog"; title: string; body: React.ReactNode }
  | { kind: "oauth"; url: string; status: string; done: boolean }
  | { kind: "models"; title: string; rows: ModelTestRow[] }
  | null;

export function ProvidersView() {
  const [providers, setProviders] = useState<ProviderDef[]>([]);
  const [conns, setConns] = useState<Connection[]>([]);
  const [quota, setQuota] = useState<QuotaSnapshot | null>(null);
  const [pools, setPools] = useState<PoolRow[]>([]);
  const [panel, setPanel] = useState<Panel>(null);
  const [err, setErr] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  async function reload(force = false) {
    try {
      const [p, c, q, pl] = await Promise.all([
        api<ProviderDef[]>("/api/providers"),
        api<Connection[]>("/api/connections"),
        quotaSnapshot(force),
        api<PoolRow[]>("/api/proxy-pools").catch(() => [] as PoolRow[]),
      ]);
      setProviders(p); setConns(c); setQuota(q); setPools(pl); setErr(null);
    } catch (e) {
      setErr((e as ApiError).message);
    }
  }
  useEffect(() => {
    reload();
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // OAuth+ : core yang pegang session + persistensi; link authorize datang
  // utuh dari paket provider. Halaman poll pakai interval vendor sendiri dan
  // menggambar ulang kartu begitu akun mendarat (sama seperti perilaku lama).
  async function oauthStart(p: ProviderDef) {
    if (pollRef.current) clearInterval(pollRef.current);
    setPanel({ kind: "oauth", url: "", status: t("conn.oauth_starting"), done: false });
    try {
      const started = await api<{ authorize_url: string; session_id: string; poll_interval_sec: number }>(
        `/api/oauth/${encodeURIComponent(p.id)}/start`, { method: "POST" });
      setPanel({ kind: "oauth", url: started.authorize_url, status: t("conn.oauth_waiting"), done: false });
      pollRef.current = window.setInterval(async () => {
        let step: { status: string; connection_id?: string };
        try {
          step = await api(`/api/oauth/${encodeURIComponent(p.id)}/${started.session_id}`);
        } catch (e) {
          step = { status: "failed", connection_id: undefined };
          void e;
        }
        if (step.status === "pending") return;
        if (pollRef.current) clearInterval(pollRef.current);
        if (step.status === "ready") {
          setPanel({ kind: "oauth", url: started.authorize_url,
            status: fmt("conn.oauth_linked", { id: short(step.connection_id || "") }), done: true });
          reload();
        } else {
          setPanel({ kind: "oauth", url: started.authorize_url, status: t("conn.oauth_failed"), done: true });
        }
      }, Math.max(2, started.poll_interval_sec || 5) * 1000);
    } catch (e) {
      setPanel({ kind: "oauth", url: "", status: t("conn.oauth_failed") + " — " + (e as Error).message, done: true });
    }
  }

  async function showCatalog(c: Connection) {
    setPanel({ kind: "catalog", title: fmt("conn.catalog_title", { value: short(c.id) }), body: t("ui.busy") });
    try {
      // Katalog dibaca paksa (?force=true) di sini memang sengaja: tab ini
      // tempat kita mengecek apakah vendor masih cocok dengan mirror lokal.
      const r = await api<CatalogRow>(`/api/connections/${c.id}/models?force=true`);
      const body = r.ok
        ? (
          <table>
            <thead><tr>
              <th>{t("common.model")}</th><th>{t("common.name")}</th>
              <th>{t("conn.col_context")}</th><th>{t("conn.col_vision")}</th><th>{t("conn.col_tools")}</th>
            </tr></thead>
            <tbody>
              {(r.models || []).map(m => (
                <tr key={m.id}>
                  <td className="mono tiny">{m.id}</td>
                  <td className="tiny">{m.name}</td>
                  <td className="tiny">{num(m.context_length)}</td>
                  <td className="tiny">{m.supports_vision ? t("common.yes") : "—"}</td>
                  <td className="tiny">{m.supports_tools ? t("common.yes") : "—"}</td>
                </tr>
              ))}
              {!(r.models || []).length && <EmptyState label={t("common.empty")} colSpan={5} />}
            </tbody>
          </table>
        )
        : <p className="tiny err-text">{r.error || t("common.failed")}</p>;
      setPanel({ kind: "catalog", title: fmt("conn.catalog_title", { value: short(c.id) }), body });
    } catch (e) {
      setPanel({ kind: "catalog", title: fmt("conn.catalog_title", { value: short(c.id) }),
        body: <p className="tiny err-text">{(e as ApiError).message}</p> });
    }
  }

  async function testModels(c: Connection) {
    if (!window.confirm(t("conn.test_models_confirm"))) return;
    setPanel({ kind: "models", title: fmt("conn.test_models_title", { value: short(c.id) }), rows: [] });
    try {
      const r = await api<{ results: ModelTestRow[] }>(
        `/api/connections/${c.id}/test_models`,
        { method: "POST", body: JSON.stringify({ allow_spend: true, max_models: 2 }) });
      setPanel({ kind: "models", title: fmt("conn.test_models_title", { value: short(c.id) }), rows: r.results || [] });
    } catch (e) {
      setPanel({ kind: "models", title: fmt("conn.test_models_title", { value: short(c.id) }),
        rows: [{ model: "—", ok: false, latency_ms: null, error_class: (e as ApiError).message }] });
    }
  }

  const qByConn: Record<string, import("./types").QuotaReading[]> = {};
  (quota?.providers || []).forEach(p => { if (p.available) qByConn[p.connection_id] = p.readings || []; });
  const byProv: Record<string, Connection[]> = {};
  conns.forEach(c => { (byProv[c.provider] = byProv[c.provider] || []).push(c); });

  return (
    <div>
      <div className="row sec-tools">
        <button className="tight" onClick={() => reload(true)} disabled={!token()}>{t("conn.refresh_quota")}</button>
        <InlineMsg msg={err} kind="err" />
      </div>

      {providers.map(p => (
        <ProviderCard
          key={p.id} def={p} accounts={byProv[p.id] || []} pools={pools}
          readings={qByConn} onOauth={() => oauthStart(p)} onCatalog={showCatalog}
          onTestModels={testModels} onReload={() => reload()}
        />
      ))}

      {panel && (
        <Card title={panel.kind === "catalog" ? panel.title : panel.kind === "models" ? panel.title : t("conn.oauth_add")}>
          {panel.kind === "oauth" && (
            <div>
              {panel.url && (
                <p className="tiny">
                  <a href={panel.url} target="_blank" rel="noreferrer">{panel.url}</a>
                  {" "}<button className="tiny" onClick={() => { navigator.clipboard?.writeText(panel.url || ""); }}>{t("common.copy")}</button>
                </p>
              )}
              <p className="tiny muted">{panel.status}{!panel.done && panel.url ? "" : ""}</p>
            </div>
          )}
          {panel.kind === "catalog" && panel.body}
          {panel.kind === "models" && (
            <table>
              <thead><tr>
                <th>{t("common.model")}</th><th>{t("common.col_result")}</th><th>{t("conn.col_latency")}</th><th>{t("common.error")}</th>
              </tr></thead>
              <tbody>
                {panel.rows.map(x => (
                  <tr key={x.model}>
                    <td className="mono tiny">{x.model}</td>
                    <td><Badge status={x.ok ? "ok" : "upstream_error"} /></td>
                    <td className="tiny">{num(x.latency_ms)} ms</td>
                    <td className="tiny mono">{x.error_class || ""}</td>
                  </tr>
                ))}
                {!panel.rows.length && <EmptyState label={t("ui.busy")} colSpan={4} />}
              </tbody>
            </table>
          )}
        </Card>
      )}

      <AddManualCard providers={providers} pools={pools} onCreated={() => reload()} />
    </div>
  );
}

function ProviderCard(props: {
  def: ProviderDef;
  accounts: Connection[];
  pools: PoolRow[];
  readings: Record<string, import("./types").QuotaReading[]>;
  onOauth: () => void;
  onCatalog: (c: Connection) => void;
  onTestModels: (c: Connection) => void;
  onReload: () => void;
}) {
  const p = props.def;
  const [oauthBusy, setOauthBusy] = useState(false);
  return (
    <Card
      title={<span>{p.display_name || p.id}{p.display_name && !p.display_name.toLowerCase().includes(p.id) ? <> <span className="mono muted tiny">{p.id}</span></> : null}</span>}
      tools={p.oauth ? (
        <button
          className="tiny primary oauth"
          disabled={oauthBusy}
          onClick={async () => { setOauthBusy(true); try { await props.onOauth(); } finally { setOauthBusy(false); } }}
        >{t("conn.oauth_add")}</button>
      ) : null}
    >
      <div className="prov-meta row">
        <Meta k="conn.col_auth" v={p.auth_modes.join(" · ") || "—"} />
        <Meta k="conn.col_probe" v={p.probe_tier} />
        <Meta k="providers.base_url" v={<span className="mono tiny" title={p.base_url}>{p.base_url}</span>} />
        {p.is_node && <span className="badge info" title={t("providers.node_note")}>{t("conn.node_badge")}</span>}
        {p.has_usage && <span className="badge violet">{t("providers.usage_badge")}</span>}
      </div>

      {!props.accounts.length && <EmptyState label={t("conn.no_accounts")} />}

      {props.accounts.map(c => (
        <AccountRow
          key={c.id} c={c} pools={props.pools}
          readings={props.readings[c.id] || []}
          onCatalog={() => props.onCatalog(c)}
          onTestModels={() => props.onTestModels(c)}
          onReload={props.onReload}
        />
      ))}
    </Card>
  );
}

function AccountRow(props: {
  c: Connection;
  pools: PoolRow[];
  readings: import("./types").QuotaReading[];
  onCatalog: () => void;
  onTestModels: () => void;
  onReload: () => void;
}) {
  const c = props.c;
  const h: Partial<Health> = c.health || {};
  const [testMsg, setTestMsg] = useState<string | null>(null);
  const [confirmDel, setConfirmDel] = useState(false);
  const [editing, setEditing] = useState(false);

  async function runTest() {
    setTestMsg(t("ui.busy"));
    try {
      const r = await api<{ ok: boolean; latency_ms?: number; error?: string }>(
        `/api/connections/${c.id}/test`, { method: "POST" });
      setTestMsg(r.ok ? fmt("common.ok_ms", { value: r.latency_ms ?? 0 }) : t("common.failed") + (r.error ? " · " + r.error : ""));
      props.onReload();
    } catch (e) {
      setTestMsg(t("common.error") + " · " + (e as ApiError).message);
    }
  }

  async function remove() {
    try {
      await api(`/api/connections/${c.id}`, { method: "DELETE" });
      props.onReload();
    } catch (e) {
      setTestMsg(t("common.error") + " · " + (e as ApiError).message);
    }
    setConfirmDel(false);
  }

  const poolName = c.proxy_pool_id
    ? (props.pools.find(x => x.id === c.proxy_pool_id)?.name || short(c.proxy_pool_id))
    : null;

  return (
    <div className="acct">
      <div className="acct-head">
        <span className="mono">{c.name || short(c.id)}</span>
        <Badge status={h.test_status || "unknown"} />
        {h.error_class && <span className="tiny muted">{h.error_class}</span>}
        <span className="tiny muted">{c.is_active ? t("common.active") : t("common.paused")}</span>
        <Meta k="conn.col_priority" v={num(c.priority)} />
        {poolName && <Meta k="conn.col_proxy" v={poolName} />}
        {c.expires_at
          ? <Meta k="conn.expires_in" v={dcount(c.expires_at)} />
          : c.has_api_key ? <Meta k="conn.api_key_label" v={<span className="badge ok">{t("conn.api_key_present")}</span>} /> : null}
        {(h.rate_limited_until || 0) > Date.now() && (
          <Meta k="conn.rate_limited_until" v={dcount(h.rate_limited_until)} />
        )}
        <span className="acct-actions row tight">
          <button className="tiny" onClick={runTest}>{t("conn.test")}</button>
          <button className="tiny" onClick={props.onCatalog}>{t("conn.catalog")}</button>
          <button className="tiny" onClick={props.onTestModels}>{t("conn.test_models")}</button>
          <button className="tiny" onClick={() => setEditing(v => !v)}>{t("common.edit")}</button>
          {confirmDel
            ? <ConfirmBar text={t("conn.delete_confirm")} onYes={remove} onNo={() => setConfirmDel(false)} />
            : <button className="tiny danger" onClick={() => setConfirmDel(true)}>{t("common.delete")}</button>}
        </span>
      </div>
      <InlineMsg msg={testMsg} kind={testMsg && testMsg.startsWith(t("common.error")) ? "err" : "ok"} />
      {editing && <EditConnForm c={c} pools={props.pools} onDone={() => { setEditing(false); props.onReload(); }} />}
      <div className="acct-quota">
        <QuotaGroup readings={props.readings} />
      </div>
    </div>
  );
}

// Edit koneksi: hanya field milik operator (bukan kredensial) -- prioritas,
// pool proxy, status aktif; kredensial lewat OAuth+ / import script.
function EditConnForm(props: { c: Connection; pools: PoolRow[]; onDone: () => void }) {
  const [name, setName] = useState(props.c.name || "");
  const [priority, setPriority] = useState(String(props.c.priority ?? 1));
  const [pool, setPool] = useState(props.c.proxy_pool_id || "");
  const [active, setActive] = useState(props.c.is_active);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function save() {
    setBusy(true); setErr(null);
    try {
      await api(`/api/connections/${props.c.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: name || undefined,
          priority: Number(priority),
          proxy_pool_id: pool || null,
          is_active: active,
        }),
      });
      props.onDone();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="form-inline">
      <label>{t("common.name")}<input value={name} onChange={e => setName(e.target.value)} placeholder={t("conn.name_ph")} /></label>
      <label>{t("conn.col_priority")}<input value={priority} onChange={e => setPriority(e.target.value)} size={4} /></label>
      <label>{t("conn.col_proxy")}
        <select value={pool} onChange={e => setPool(e.target.value)}>
          <option value="">—</option>
          {props.pools.map(x => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
      </label>
      <label className="chk"><input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} /> {t("common.active")}</label>
      <button className="tiny primary" disabled={busy} onClick={save}>{busy ? t("ui.busy") : t("common.save")}</button>
      <InlineMsg msg={err} kind="err" />
    </div>
  );
}

// Tambah akun manual (mode apikey / token) -- bentuk payload mengikuti
// POST /api/connections (admin_connections.create_connection): provider,
// auth_type, name, api_key, priority, probe.
function AddManualCard(props: { providers: ProviderDef[]; pools: PoolRow[]; onCreated: () => void }) {
  const [provider, setProvider] = useState("");
  const [authType, setAuthType] = useState("apikey");
  const [name, setName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [pool, setPool] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; err: boolean } | null>(null);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true); setNote(null);
    try {
      const row = await api<{ id: string; probe?: { ok: boolean; error?: string } }>("/api/connections", {
        method: "POST",
        body: JSON.stringify({
          provider, auth_type: authType, name: name || undefined,
          api_key: apiKey || undefined,
          proxy_pool_id: pool || undefined,
          priority: 1,
        }),
      });
      const probe = row.probe;
      setNote({
        text: fmt("conn.created", { id: short(row.id) })
          + (probe ? " · " + fmt("conn.probe_result", { value: probe.ok ? t("common.ok") : (probe.error || t("common.failed")) }) : ""),
        err: !!probe && !probe.ok,
      });
      if (!probe || probe.ok) { setName(""); setApiKey(""); props.onCreated(); }
    } catch (e) {
      setNote({ text: (e as ApiError).message, err: true });
    } finally {
      setBusy(false);
    }
  }

  const candidates = props.providers.filter(p => p.auth_modes.includes("apikey") || p.auth_modes.includes("token"));
  return (
    <Card title={t("conn.add_manual")}>
      <form className="grid" onSubmit={submit}>
        <label>{t("common.provider")}
          <select value={provider} onChange={e => setProvider(e.target.value)} required>
            <option value="">{t("conn.provider_ph")}</option>
            {candidates.map(p => <option key={p.id} value={p.id}>{p.display_name} ({p.id})</option>)}
          </select>
        </label>
        <label>{t("conn.col_auth")}
          <select value={authType} onChange={e => setAuthType(e.target.value)}>
            <option value="apikey">apikey</option>
            <option value="token">token</option>
          </select>
        </label>
        <label>{t("common.name")}<input value={name} onChange={e => setName(e.target.value)} placeholder={t("conn.name_ph")} /></label>
        <label>{t("conn.f_api_key")}<input value={apiKey} onChange={e => setApiKey(e.target.value)} type="password" autoComplete="off" /></label>
        <label>{t("conn.col_proxy")}
          <select value={pool} onChange={e => setPool(e.target.value)}>
            <option value="">—</option>
            {props.pools.map(x => <option key={x.id} value={x.id}>{x.name}</option>)}
          </select>
        </label>
        <div className="tight"><button className="primary" disabled={busy || !provider}>{busy ? t("ui.busy") : t("common.add")}</button></div>
      </form>
      {note && <InlineMsg msg={note.text} kind={note.err ? "err" : "ok"} />}
      <p className="tiny muted">{t("conn.manual_note")}</p>
    </Card>
  );
}
