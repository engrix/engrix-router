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
  Badge, Card, ConfirmBar, EmptyState, InlineMsg, Meta, ProviderLogo, QuotaGroup, Switch,
} from "./components";
import type {
  BackupDoc, BackupImportResult, BulkResult, CatalogRow, Connection, Health, ModelTestRow,
  PoolRow, ProviderDef, QuotaSnapshot,
} from "./types";

type Panel = { kind: "catalog"; brand: string; title: string; body: React.ReactNode }
  | { kind: "oauth"; brand: string; url: string; status: string; done: boolean }
  | { kind: "models"; brand: string; title: string; rows: ModelTestRow[] }
  | null;

export function ProvidersView(props: {
  focusBrand?: string;
  onOpenBrand: (brand: string) => void;
  onBack: () => void;
}) {
  const focus = props.focusBrand || "";
  const [providers, setProviders] = useState<ProviderDef[]>([]);
  const [conns, setConns] = useState<Connection[]>([]);
  const [quota, setQuota] = useState<QuotaSnapshot | null>(null);
  const [pools, setPools] = useState<PoolRow[]>([]);
  const [panel, setPanel] = useState<Panel>(null);
  const [err, setErr] = useState<string | null>(null);
  const [bulkMsg, setBulkMsg] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(0);
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

  // Auto-refresh (padanan "Auto-refresh" di Quota Tracker 9router): muat ulang
  // kuota + koneksi tiap N detik. force=true supaya angka di layar benar-benar
  // segar, bukan cache 25 detik -- tombol ini memang buat dipantau dari jauh.
  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => { void reload(true); }, autoRefresh * 1000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRefresh]);

  // OAuth+ : core yang pegang session + persistensi; link authorize datang
  // utuh dari paket provider. Halaman poll pakai interval vendor sendiri dan
  // menggambar ulang kartu begitu akun mendarat (sama seperti perilaku lama).
  async function oauthStart(p: ProviderDef) {
    if (pollRef.current) clearInterval(pollRef.current);
    setPanel({ kind: "oauth", brand: p.brand || p.display_name || p.id, url: "", status: t("conn.oauth_starting"), done: false });
    try {
      const started = await api<{ authorize_url: string; session_id: string; poll_interval_sec: number }>(
        `/api/oauth/${encodeURIComponent(p.id)}/start`, { method: "POST" });
      setPanel({ kind: "oauth", brand: p.brand || p.display_name || p.id, url: started.authorize_url, status: t("conn.oauth_waiting"), done: false });
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
          setPanel({ kind: "oauth", brand: p.brand || p.display_name || p.id, url: started.authorize_url,
            status: fmt("conn.oauth_linked", { id: short(step.connection_id || "") }), done: true });
          reload();
        } else {
          setPanel({ kind: "oauth", brand: p.brand || p.display_name || p.id, url: started.authorize_url, status: t("conn.oauth_failed"), done: true });
        }
      }, Math.max(2, started.poll_interval_sec || 5) * 1000);
    } catch (e) {
      setPanel({ kind: "oauth", brand: p.brand || p.display_name || p.id, url: "", status: t("conn.oauth_failed") + " — " + (e as Error).message, done: true });
    }
  }

  async function showCatalog(c: Connection) {
    const brand = brandOf(c.provider);
    setPanel({ kind: "catalog", brand, title: fmt("conn.catalog_title", { value: short(c.id) }), body: t("ui.busy") });
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
      setPanel({ kind: "catalog", brand, title: fmt("conn.catalog_title", { value: short(c.id) }), body });
    } catch (e) {
      setPanel({ kind: "catalog", brand, title: fmt("conn.catalog_title", { value: short(c.id) }),
        body: <p className="tiny err-text">{(e as ApiError).message}</p> });
    }
  }

  async function testModels(c: Connection) {
    if (!window.confirm(t("conn.test_models_confirm"))) return;
    const brand = brandOf(c.provider);
    setPanel({ kind: "models", brand, title: fmt("conn.test_models_title", { value: short(c.id) }), rows: [] });
    try {
      const r = await api<{ results: ModelTestRow[] }>(
        `/api/connections/${c.id}/test_models`,
        { method: "POST", body: JSON.stringify({ allow_spend: true, max_models: 2 }) });
      setPanel({ kind: "models", brand, title: fmt("conn.test_models_title", { value: short(c.id) }), rows: r.results || [] });
    } catch (e) {
      setPanel({ kind: "models", brand, title: fmt("conn.test_models_title", { value: short(c.id) }),
        rows: [{ model: "—", ok: false, latency_ms: null, error_class: (e as ApiError).message }] });
    }
  }

  // Bulk on/off per merek: satu merek bisa punya beberapa provider id, jadi
  // aksi dijalankan per id lalu dijumlah. Aksi berbasis kuota menilai dari
  // snapshot terbaru (sama seperti yang barusan dibaca di tracker), bukan panggilan
  // vendor baru -- lihat catatan di api/admin_connections.py bulk_connections.
  async function runBulk(action: string, providerIds: string[]) {
    if (action === "deactivate" && !window.confirm(t("bulk.all_off_confirm"))) return;
    setBulkBusy(true); setBulkMsg(null);
    let changed = 0, unchanged = 0, noQuota = 0, total = 0;
    try {
      for (const providerId of providerIds) {
        const r = await api<BulkResult>("/api/connections/bulk", {
          method: "POST", body: JSON.stringify({ action, provider: providerId }),
        });
        changed += r.changed; unchanged += r.unchanged;
        noQuota += r.no_quota_data; total += r.total;
      }
      setBulkMsg(fmt("bulk.result", { action, changed, unchanged, no_quota: noQuota, total }));
    } catch (e) {
      setBulkMsg(t("common.error") + " · " + (e as ApiError).message);
    } finally {
      setBulkBusy(false);
    }
  }

  const qByConn: Record<string, import("./types").QuotaReading[]> = {};
  (quota?.providers || []).forEach(p => { if (p.available) qByConn[p.connection_id] = p.readings || []; });
  const byProv: Record<string, Connection[]> = {};
  conns.forEach(c => { (byProv[c.provider] = byProv[c.provider] || []).push(c); });

  // Brand grouping (TASK-45/46): dashboard diurut PER MEREK. List = satu
  // baris per merek; klik buka halaman merek itu (route providers/<brand>).
  // Alasan skala: 1 merek bisa punya banyak varian (Qoder intl + CN, ZCode
  // start + coding) dan ribuan akun; satu halaman flat = scroll muntah.
  const brandMap: Record<string, ProviderDef[]> = {};
  providers.forEach(p => {
    const b = p.brand || p.display_name || p.id;
    (brandMap[b] = brandMap[b] || []).push(p);
  });
  const brandOf = (providerId: string): string => {
    const p = providers.find(x => x.id === providerId);
    return p ? (p.brand || p.display_name || p.id) : providerId;
  };
  const brands = Object.entries(brandMap).sort((a, b) => a[0].localeCompare(b[0]));

  return (
    <div>
      {/* Breadcrumb dua aras (TASK-46): list merek -> halaman satu merek.
          Kalau fokus aktif, hanya varian merek itu yang dirender + tombol
          kembali; kalau tidak, list merek ringkas (satu baris per merek). */}
      {focus && (
        <div className="row sec-tools">
          <button className="tight" onClick={props.onBack}>← {t("nav.providers")}</button>
          <h2 className="crumb">{focus}</h2>
        </div>
      )}
      <div className="row sec-tools">
        <button className="tight" onClick={() => reload(true)} disabled={!token()}>{t("conn.refresh_quota")}</button>
        <label className="tiny muted row tight">
          {t("ui.auto_refresh")}
          <select value={autoRefresh} onChange={e => setAutoRefresh(Number(e.target.value))}>
            <option value={0}>{t("ui.auto_refresh_off")}</option>
            <option value={30}>{t("ui.auto_refresh_30s")}</option>
            <option value={60}>{t("ui.auto_refresh_60s")}</option>
          </select>
        </label>
        <InlineMsg msg={bulkMsg} kind={bulkMsg && bulkMsg.startsWith(t("common.error")) ? "err" : "ok"} />
        <InlineMsg msg={err} kind="err" />
      </div>

      {/* Mode daftar = GRID kartu bermerek (logo + status akun), bukan baris teks:
          mata langsung nemu vendor mana yang nyambung tanpa baca satu per satu.
          Klik kartu = halaman merek itu (route providers/<brand>). */}
      {!focus && (
        <div className="brand-grid">
          {brands.map(([brand, defs]) => {
            const acctCount = defs.reduce((n, d) => n + (byProv[d.id] || []).length, 0);
            const onlineCount = defs.reduce((n, d) => n + (d.active_connections || 0), 0);
            const models = new Set(defs.flatMap(d => d.models_named.map(m => m.name || m.id)));
            const logoId = _logoProviderId(brand, defs);
            return (
              <button key={brand} type="button" className="brand-tile"
                onClick={() => props.onOpenBrand(brand)} title={fmt("providers.open_brand", { value: brand })}>
                <span className="brand-tile-top">
                  <ProviderLogo id={logoId} brand={brand} size="md" />
                  <span className="brand-tile-name">{brand}</span>
                  <span className="brand-tile-caret">▸</span>
                </span>
                <span className="brand-tile-status">
                  {acctCount > 0
                    ? <><span className="dot ok" aria-hidden="true" />{_accountsLabel(acctCount)}</>
                    : <><span className="dot" aria-hidden="true" />{t("common.no_connections")}</>}
                </span>
                <span className="brand-tile-stats tiny muted">
                  {defs.length > 1 && <span>{fmt("providers.variants_n", { value: defs.length })}</span>}
                  {onlineCount > 0 && onlineCount !== acctCount &&
                    <span>{fmt("providers.active_n", { value: onlineCount })}</span>}
                  {models.size > 0 && <span>{_modelsLabel(models.size)}</span>}
                </span>
              </button>
            );
          })}
        </div>
      )}

      {brands.map(([brand, defs]) => {
        if (!focus || focus !== brand) return null;
        const acctCount = defs.reduce((n, d) => n + (byProv[d.id] || []).length, 0);
        const models = new Set(defs.flatMap(d => d.models_named.map(m => m.name || m.id)));
        return (
          <section key={brand} className="card brand open">
            <header className="card-head">
              <h3 className="brand-title">
                <ProviderLogo id={_logoProviderId(brand, defs)} brand={brand} size="md" /> {brand}
              </h3>
              <div className="row tight">
                <span className="badge" title={t("providers.variants")} aria-label="variants">{defs.length}</span>
                <span className="badge violet" title={t("providers.accounts")} aria-label="accounts">{acctCount}</span>
                {models.size > 0 && <span className="badge info" title={t("providers.models_known")} aria-label="models">{models.size}</span>}
                <BulkBar providerIds={defs.map(d => d.id)} busy={bulkBusy} onRun={runBulk} />
              </div>
            </header>
            {defs.map(p => (
              <ProviderCard
                key={p.id} def={p} accounts={byProv[p.id] || []} pools={pools}
                readings={qByConn} onOauth={() => oauthStart(p)} onCatalog={showCatalog}
                onTestModels={testModels} onReload={() => reload()}
              />
            ))}
            {panel && panel.brand === brand && (
              <PanelBox panel={panel} />
            )}
          </section>
        );
      })}
      {!providers.length && <EmptyState label={t("common.empty")} />}

      {!focus && <BackupCard onDone={() => reload()} />}
      {!focus && <AddManualCard providers={providers} pools={pools} onCreated={() => reload()} />}
    </div>
  );
}

// Id provider yang dipakai buat gambar logo satu merek. Merek bisa punya beberapa
// varian (qoder + qoder-cn); logo merek diambil dari varian pertama, bukan dari
// varian yang kebetulan punya akun -- biar gambarnya stabil walau akun pindah.
function _logoProviderId(_brand: string, defs: ProviderDef[]): string {
  return defs[0]?.id || "";
}

// "1 accounts" kebaca seperti bug. Katalog string tetap satu sumber: bentuk jamak
// hanya dipilih di sini, angkanya tetap dari katalog.
function _accountsLabel(count: number): string {
  return count === 1 ? t("providers.accounts_one") : fmt("providers.accounts_n", { value: count });
}

function _modelsLabel(count: number): string {
  return count === 1 ? t("providers.models_one") : fmt("providers.models_n", { value: count });
}

// Kotak hasil (katalog / tes model / OAuth+) -- dipindah ke dalam merek
// pemiliknya supaya konteks tidak tercerai (dulu nyangkut di bawah semua kartu).
function PanelBox(props: { panel: NonNullable<Panel> }) {
  const panel = props.panel;
  return (
    <Card
      inner
      title={panel.kind === "catalog" ? panel.title : panel.kind === "models" ? panel.title : t("conn.oauth_add")}
    >
      {panel.kind === "oauth" && (
        <div>
          {panel.url && (
            <p className="tiny">
              <a href={panel.url} target="_blank" rel="noreferrer">{panel.url}</a>
              {" "}<button className="tiny" onClick={() => { navigator.clipboard?.writeText(panel.url || ""); }}>{t("common.copy")}</button>
            </p>
          )}
          <p className="tiny muted">{panel.status}</p>
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
      inner
      title=<span className="prov-title">
        <ProviderLogo id={p.id} brand={p.brand} />
        <span>{p.display_name || p.id}</span>
        {p.display_name && !p.display_name.toLowerCase().includes(p.id)
          ? <span className="mono muted tiny">{p.id}</span> : null}
      </span>
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

      {/* Daftar model dikenal (deklarasi adapter; tanpa manggil vendor),
          supaya user tahu model apa saja yang bisa diminta ke varian ini. */}
      {p.models_named.length > 0 && (
        <div className="row tight prov-models">
          <span className="tiny muted">{t("providers.models_known")}:</span>
          {p.models_named.map(m => (
            <span key={m.id} className="badge" title={m.id}>{m.name || m.id}</span>
          ))}
        </div>
      )}

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
  // Status aktif dipegang lokal supaya saklar langsung berubah saat diketuk
  // (dashboard 9router: toggle instan). Kalau PATCH gagal, posisinya dibalikin dan
  // pesan error vendor/server tampil apa adanya di baris ini -- bukan di-swallow.
  const [active, setActive] = useState(c.is_active);
  const [toggling, setToggling] = useState(false);
  useEffect(() => { setActive(c.is_active); }, [c.is_active]);

  async function toggle(next: boolean) {
    setActive(next); setToggling(true); setTestMsg(null);
    try {
      await api(`/api/connections/${c.id}`, {
        method: "PATCH", body: JSON.stringify({ is_active: next }),
      });
      props.onReload();
    } catch (e) {
      setActive(!next);
      setTestMsg(t("common.error") + " · " + (e as ApiError).message);
    } finally {
      setToggling(false);
    }
  }

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
        <Switch
          checked={active} onChange={toggle} disabled={toggling}
          label={active ? t("common.disable") : t("common.enable")}
        />
        <span className="tiny muted">{active ? t("common.active") : t("common.paused")}</span>
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
      {/* Alasan gagal terakhir = kalimat vendor apa adanya (health.last_error tidak
          pernah ditulis ulang gateway). `title` bikin teks panjang tetap kebaca. */}
      {h.last_error && (
        <p className="tiny acct-error" title={h.last_error}>
          <span className="metalabel">{t("common.error")}</span> {h.last_error}
        </p>
      )}
      {editing && <EditConnForm c={c} pools={props.pools} onDone={() => { setEditing(false); props.onReload(); }} />}
      <div className="acct-quota">
        <QuotaGroup readings={props.readings} />
      </div>
    </div>
  );
}

// Edit koneksi: field milik operator (prioritas, pool proxy, status aktif)
// plus patch token opsional -- access_token baru (mis. cookie Muse yang
// kadaluarsa) ditempel di sini, kosong = kredensial lama dibiarkan utuh.
function EditConnForm(props: { c: Connection; pools: PoolRow[]; onDone: () => void }) {
  const [name, setName] = useState(props.c.name || "");
  const [priority, setPriority] = useState(String(props.c.priority ?? 1));
  const [pool, setPool] = useState(props.c.proxy_pool_id || "");
  const [active, setActive] = useState(props.c.is_active);
  const [newToken, setNewToken] = useState("");
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
          access_token: newToken || undefined,
        }),
      });
      setNewToken("");
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
      <label>{t("conn.f_new_token")}
        <input value={newToken} onChange={e => setNewToken(e.target.value)} type="password" autoComplete="off" placeholder={t("conn.f_new_token_ph")} />
      </label>
      <label className="chk"><input type="checkbox" checked={active} onChange={e => setActive(e.target.checked)} /> {t("common.active")}</label>
      <button className="tiny primary" disabled={busy} onClick={save}>{busy ? t("ui.busy") : t("common.save")}</button>
      <InlineMsg msg={err} kind="err" />
    </div>
  );
}

// Tambah akun manual (mode apikey / token / oauth) -- bentuk payload mengikuti
// POST /api/connections (admin_connections.create_connection): provider,
// auth_type, name, api_key / access_token, provider_specific, priority, probe.
// Mode oauth = provider yang alur link-nya selesai di luar router (mis. muse:
// cookie abra_sess dari browser yang sudah login) -- token ditempel langsung,
// tanpa relay device-code; provider_specific dikirim sebagai objek JSON.
function AddManualCard(props: { providers: ProviderDef[]; pools: PoolRow[]; onCreated: () => void }) {
  const [provider, setProvider] = useState("");
  const [authType, setAuthType] = useState("apikey");
  const [name, setName] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [psdJson, setPsdJson] = useState("");
  const [pool, setPool] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; err: boolean } | null>(null);

  // Provider yang HANYA punya mode oauth (mis. muse) langsung dimap ke oauth --
  // operator gak perlu nebak auth yang bener, tinggal pilih provider + tempel.
  function pickProvider(id: string) {
    setProvider(id);
    const def = props.providers.find(x => x.id === id);
    if (def && !def.auth_modes.includes("apikey") && !def.auth_modes.includes("token")
      && def.auth_modes.includes("oauth")) {
      setAuthType("oauth");
    }
  }

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    let psd: Record<string, unknown> | undefined;
    if (psdJson.trim()) {
      try {
        const parsed: unknown = JSON.parse(psdJson);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not-object");
        psd = parsed as Record<string, unknown>;
      } catch {
        setNote({ text: t("conn.f_psd_invalid"), err: true });
        return;
      }
    }
    setBusy(true); setNote(null);
    try {
      const row = await api<{ id: string; probe?: { ok: boolean; error?: string } }>("/api/connections", {
        method: "POST",
        body: JSON.stringify({
          provider, auth_type: authType, name: name || undefined,
          api_key: authType === "oauth" ? undefined : (apiKey || undefined),
          access_token: authType === "oauth" ? (accessToken || undefined) : undefined,
          provider_specific: psd,
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
      if (!probe || probe.ok) { setName(""); setApiKey(""); setAccessToken(""); setPsdJson(""); props.onCreated(); }
    } catch (e) {
      setNote({ text: (e as ApiError).message, err: true });
    } finally {
      setBusy(false);
    }
  }

  const candidates = props.providers.filter(p =>
    p.auth_modes.includes("apikey") || p.auth_modes.includes("token") || p.auth_modes.includes("oauth"));
  return (
    <Card title={t("conn.add_manual")}>
      <form className="grid" onSubmit={submit}>
        <label>{t("common.provider")}
          <select value={provider} onChange={e => pickProvider(e.target.value)} required>
            <option value="">{t("conn.provider_ph")}</option>
            {candidates.map(p => <option key={p.id} value={p.id}>{p.display_name} ({p.id})</option>)}
          </select>
        </label>
        <label>{t("conn.col_auth")}
          <select value={authType} onChange={e => setAuthType(e.target.value)}>
            <option value="apikey">apikey</option>
            <option value="token">token</option>
            <option value="oauth">oauth</option>
          </select>
        </label>
        <label>{t("common.name")}<input value={name} onChange={e => setName(e.target.value)} placeholder={t("conn.name_ph")} /></label>
        {authType === "oauth" ? (
          <>
            <label>{t("conn.f_access_token")}<input value={accessToken} onChange={e => setAccessToken(e.target.value)} type="password" autoComplete="off" placeholder={t("conn.f_access_token_ph")} /></label>
            <label>{t("conn.f_psd_json")}
              <textarea value={psdJson} onChange={e => setPsdJson(e.target.value)} rows={3} className="mono" placeholder={t("conn.f_psd_ph")} />
            </label>
          </>
        ) : (
          <label>{t("conn.f_api_key")}<input value={apiKey} onChange={e => setApiKey(e.target.value)} type="password" autoComplete="off" /></label>
        )}
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

// Bulk on/off merek ini: matiin yang kuotanya habis, nyalain yang ada kuota,
// plus saklar semua. Nama aksi persis dengan API (activate_available,
// deactivate_empty, activate, deactivate) supaya log dan UI sepaham.
function BulkBar(props: {
  providerIds: string[];
  busy: boolean;
  onRun: (action: string, providerIds: string[]) => void;
}) {
  const ids = props.providerIds;
  return (
    <span className="row tight">
      <button className="tiny" disabled={props.busy}
        onClick={() => props.onRun("activate_available", ids)}>{t("bulk.turn_on_available")}</button>
      <button className="tiny" disabled={props.busy}
        onClick={() => props.onRun("deactivate_empty", ids)}>{t("bulk.turn_off_empty")}</button>
      <button className="tiny" disabled={props.busy}
        onClick={() => props.onRun("activate", ids)}>{t("bulk.all_on")}</button>
      <button className="tiny danger" disabled={props.busy}
        onClick={() => props.onRun("deactivate", ids)}>{t("bulk.all_off")}</button>
    </span>
  );
}

// Backup akun: keluarnya JSON (semua akun + token -- memang tujuan backup),
// masukannya JSON tempelan atau berkas. Mode merge memperbarui yang sudah ada
// (cocokkan id lalu provider+nama) dan membuat sisanya; mode replace menghapus
// dulu -- karena itu dia minta konfirmasi kedua. Kunci gaya 9router
// (accessToken/providerSpecificData/isActive) diterima apa adanya oleh server.
function BackupCard(props: { onDone: () => void }) {
  const [text, setText] = useState("");
  const [mode, setMode] = useState("merge");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ text: string; err: boolean } | null>(null);

  async function exportNow() {
    setBusy(true); setNote(null);
    try {
      const doc = await api<BackupDoc>("/api/backup");
      const blob = new Blob([JSON.stringify(doc, null, 1)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `engrix-router-backup-${new Date().toISOString().slice(0, 10)}.json`;
      link.click();
      URL.revokeObjectURL(url);
      setNote({ text: fmt("backup.exported", { value: doc.connections.length }), err: false });
    } catch (e) {
      setNote({ text: (e as ApiError).message, err: true });
    } finally {
      setBusy(false);
    }
  }

  async function importNow() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setNote({ text: t("conn.f_psd_invalid"), err: true });
      return;
    }
    const connections = (parsed as { connections?: unknown } | null)?.connections;
    if (!Array.isArray(connections)) {
      setNote({ text: t("backup.bad_shape"), err: true });
      return;
    }
    if (mode === "replace" && !window.confirm(t("backup.replace_confirm"))) return;
    setBusy(true); setNote(null);
    try {
      const r = await api<BackupImportResult>("/api/backup/import", {
        method: "POST",
        body: JSON.stringify({ mode, connections, ...(mode === "replace" ? { confirm_replace: true } : {}) }),
      });
      setNote({
        text: fmt("backup.imported", { created: r.created, updated: r.updated, skipped: r.skipped }),
        err: r.skipped > 0,
      });
      if (r.created || r.updated) props.onDone();
    } catch (e) {
      setNote({ text: (e as ApiError).message, err: true });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={t("backup.title")}>
      <div className="row tight sec-tools">
        <button className="tiny primary" disabled={busy} onClick={exportNow}>{t("backup.export")}</button>
        <span className="tiny muted">{t("backup.export_hint")}</span>
      </div>
      <div className="form-inline">
        <label>{t("backup.mode")}
          <select value={mode} onChange={e => setMode(e.target.value)}>
            <option value="merge">{t("backup.mode_merge")}</option>
            <option value="replace">{t("backup.mode_replace")}</option>
          </select>
        </label>
      </div>
      <label className="tiny muted">{t("backup.paste")}
        <textarea value={text} onChange={e => setText(e.target.value)} rows={4} className="mono"
          placeholder={t("backup.paste_ph")} />
      </label>
      <div className="row tight">
        <button className="tiny" disabled={busy || !text.trim()} onClick={importNow}>{t("backup.import")}</button>
        <input type="file" accept=".json,application/json" className="tiny"
          onChange={async (e) => {
            const file = e.target.files?.[0];
            if (file) setText(await file.text());
            e.target.value = "";
          }} />
      </div>
      <InlineMsg msg={note?.text ?? null} kind={note?.err ? "err" : "ok"} />
      <p className="tiny muted">{t("backup.note")}</p>
    </Card>
  );
}
