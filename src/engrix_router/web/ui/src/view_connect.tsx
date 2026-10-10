// View Nodes (openai-compatible) + Keys + Proxy pools — TASK-43.
// Prinsip kartu: satu tabel terlabel + satu form kartu per aksi; hasil aksi
// muncul inline (bukan alert), dan nilai undefined tampil "—" bukan kosong.

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import { t } from "./i18n";
import { fmt } from "./i18n";
import { clock, short } from "./format";
import { Badge, Card, ConfirmBar, EmptyState, InlineMsg } from "./components";
import type { KeyRow, NodeRow, PoolRow } from "./types";

export function NodesView({ refreshKey }: { refreshKey: number }) {
  const [nodes, setNodes] = useState<NodeRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [f, setF] = useState({ prefix: "", name: "", base_url: "", api_key: "" });

  const load = useCallback(async () => {
    try { setNodes(await api<NodeRow[]>("/api/nodes")); setErr(null); }
    catch (e) { setErr((e as ApiError).message); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);

  async function validateSave(ev: React.FormEvent) {
    ev.preventDefault();
    setBusy(true); setErr(null);
    try {
      // Preflight dulu: node yang tidak menjawab /models tidak boleh masuk
      // katalog router -- verdict stage dipakai apa adanya, bukan dihias.
      const v = await api<{ ok: boolean; base_url: string; error?: string; stage?: string }>(
        "/api/nodes/validate",
        { method: "POST", body: JSON.stringify({ base_url: f.base_url, api_key: f.api_key, type: "openai-compatible" }) });
      if (!v.ok) {
        setErr(fmt("nodes.preflight_failed", { value: v.error || v.stage || "?" }));
        return;
      }
      await api("/api/nodes", {
        method: "POST",
        body: JSON.stringify({ name: f.name, prefix: f.prefix, base_url: v.base_url, type: "openai-compatible", api_type: "chat" }),
      });
      setF({ prefix: "", name: "", base_url: "", api_key: "" });
      load();
    } catch (e) { setErr((e as ApiError).message); }
    finally { setBusy(false); }
  }

  async function toggle(n: NodeRow) {
    try { await api(`/api/nodes/${n.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !n.is_active }) }); load(); }
    catch (e) { setErr((e as ApiError).message); }
  }

  return (
    <div>
      <Card title={t("nodes.title")}>
        <InlineMsg msg={err} kind="err" />
        <table>
          <thead><tr>
            <th>{t("nodes.col_prefix")}</th><th>{t("common.name")}</th>
            <th>{t("common.col_base_url")}</th><th>{t("common.col_type")}</th><th>{t("common.active")}</th><th></th>
          </tr></thead>
          <tbody>
            {nodes.map(n => (
              <tr key={n.id}>
                <td className="mono tiny">{n.prefix}</td>
                <td>{n.name}</td>
                <td className="mono tiny">{n.base_url}</td>
                <td className="tiny">{n.type}</td>
                <td className="tiny">{n.is_active ? t("common.yes") : t("common.no")}</td>
                <td className="row tight">
                  <button className="tiny" onClick={() => toggle(n)}>{n.is_active ? t("common.disable") : t("common.enable")}</button>
                </td>
              </tr>
            ))}
            {!nodes.length && <EmptyState label={t("nodes.empty")} colSpan={6} />}
          </tbody>
        </table>
      </Card>

      <Card title={t("nodes.add_title")}>
        <form className="grid" onSubmit={validateSave}>
          <label>{t("nodes.f_prefix")}<input required value={f.prefix} onChange={e => setF({ ...f, prefix: e.target.value })} placeholder={t("nodes.prefix_ph")} /></label>
          <label>{t("common.name")}<input required value={f.name} onChange={e => setF({ ...f, name: e.target.value })} placeholder="Groq" /></label>
          <label>{t("nodes.f_base_url")}<input required value={f.base_url} onChange={e => setF({ ...f, base_url: e.target.value })} placeholder="https://api.groq.com/openai/v1" /></label>
          <label>{t("nodes.f_api_key")}<input type="password" value={f.api_key} onChange={e => setF({ ...f, api_key: e.target.value })} autoComplete="off" /></label>
          <div className="tight"><button className="primary" disabled={busy}>{busy ? t("ui.busy") : t("nodes.validate_save")}</button></div>
        </form>
        <p className="tiny muted">{t("nodes.key_note")}</p>
      </Card>
    </div>
  );
}

export function KeysView({ refreshKey }: { refreshKey: number }) {
  const [keys, setKeys] = useState<KeyRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [confirmDel, setConfirmDel] = useState<string | null>(null);

  const load = useCallback(async () => {
    try { setKeys(await api<KeyRow[]>("/api/keys")); setErr(null); }
    catch (e) { setErr((e as ApiError).message); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);

  async function create(ev: React.FormEvent) {
    ev.preventDefault();
    setErr(null);
    try {
      const k = await api<{ key: string }>("/api/keys", { method: "POST", body: JSON.stringify({ name }) });
      setCreated(k.key);
      setName("");
      load();
    } catch (e) { setErr((e as ApiError).message); }
  }

  async function toggle(k: KeyRow) {
    try { await api(`/api/keys/${k.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !k.is_active }) }); load(); }
    catch (e) { setErr((e as ApiError).message); }
  }

  async function remove(id: string) {
    try { await api(`/api/keys/${id}`, { method: "DELETE" }); load(); }
    catch (e) { setErr((e as ApiError).message); }
    setConfirmDel(null);
  }

  return (
    <div>
      <Card title={t("keys.title")}>
        <InlineMsg msg={err} kind="err" />
        <table>
          <thead><tr>
            <th>{t("common.name")}</th><th>{t("keys.col_prefix")}</th><th>{t("common.active")}</th>
            <th>{t("keys.col_created")}</th><th>{t("keys.col_last_used")}</th><th></th>
          </tr></thead>
          <tbody>
            {keys.map(k => (
              <tr key={k.id}>
                <td>{k.name}</td>
                <td className="mono tiny" title={k.key_prefix}>{k.key_prefix}…</td>
                <td className="tiny">{k.is_active ? t("common.yes") : t("common.no")}</td>
                <td className="tiny">{clock(k.created_at)}</td>
                <td className="tiny">{k.last_used_at ? clock(k.last_used_at) : "—"}</td>
                <td className="row tight">
                  <button className="tiny" onClick={() => toggle(k)}>{k.is_active ? t("keys.disable") : t("keys.enable")}</button>
                  {confirmDel === k.id
                    ? <ConfirmBar text={t("keys.delete_confirm")} onYes={() => remove(k.id)} onNo={() => setConfirmDel(null)} />
                    : <button className="tiny danger" onClick={() => setConfirmDel(k.id)}>{t("common.delete")}</button>}
                </td>
              </tr>
            ))}
            {!keys.length && <EmptyState label={t("keys.empty")} colSpan={6} />}
          </tbody>
        </table>
      </Card>

      <Card title={t("keys.create_title")}>
        <form className="row tight" onSubmit={create}>
          <label className="grow">{t("keys.f_name")}<input required value={name} onChange={e => setName(e.target.value)} placeholder={t("keys.name_ph")} /></label>
          <div className="tight"><button className="primary">{t("common.create")}</button></div>
        </form>
        {created && (
          <div className="created-key">
            <p className="tiny">{t("keys.created_title")}</p>
            <pre className="code">{created}</pre>
            <p className="tiny muted">{t("keys.created_note")}</p>
          </div>
        )}
      </Card>
    </div>
  );
}

export function ProxyView({ refreshKey }: { refreshKey: number }) {
  const [pools, setPools] = useState<PoolRow[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [f, setF] = useState({ name: "", proxy_url: "", credential_hint: "" });

  const load = useCallback(async () => {
    try { setPools(await api<PoolRow[]>("/api/proxy-pools?include_usage=true")); setErr(null); }
    catch (e) { setErr((e as ApiError).message); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);

  async function create(ev: React.FormEvent) {
    ev.preventDefault();
    try {
      await api("/api/proxy-pools", { method: "POST", body: JSON.stringify(f) });
      setF({ name: "", proxy_url: "", credential_hint: "" });
      load();
    } catch (e) { setErr((e as ApiError).message); }
  }

  async function tryPool(id: string) {
    setErr(null);
    try {
      const r = await api<{ ok: boolean; error?: string; latency_ms?: number }>(`/api/proxy-pools/${id}/test`, { method: "POST", body: "{}" });
      setErr(r.ok ? fmt("common.ok_ms", { value: r.latency_ms ?? 0 }) : `${t("common.failed")} · ${r.error || "?"}`);
      load();
    } catch (e) { setErr((e as ApiError).message); }
  }

  async function remove(id: string) {
    try { await api(`/api/proxy-pools/${id}`, { method: "DELETE" }); load(); }
    catch (e) { setErr((e as ApiError).message); }
  }

  return (
    <div>
      <Card title={t("proxy.title")}>
        <InlineMsg msg={err} kind="ok" />
        <table>
          <thead><tr>
            <th>{t("common.name")}</th><th>{t("common.col_url")}</th><th>{t("common.col_type")}</th>
            <th>{t("proxy.col_strict")}</th><th>{t("common.col_result")}</th>
            <th>{t("proxy.col_bound")}</th><th></th>
          </tr></thead>
          <tbody>
            {pools.map(p => (
              <tr key={p.id}>
                <td>{p.name}</td>
                <td className="mono tiny" title={p.proxy_url}>{short(p.proxy_url, 28)}</td>
                <td className="tiny">{p.type}</td>
                <td className="tiny">{p.strict_proxy ? t("common.yes") : t("common.no")}</td>
                <td><Badge status={p.test_status || "unknown"} /> {p.last_error && <span className="tiny muted">{p.last_error}</span>}</td>
                <td className="tiny">{p.bound_connection_count ?? "—"}</td>
                <td className="row tight">
                  <button className="tiny" onClick={() => tryPool(p.id)}>{t("proxy.try")}</button>
                  <button className="tiny danger" onClick={() => remove(p.id)}>{t("common.delete")}</button>
                </td>
              </tr>
            ))}
            {!pools.length && <EmptyState label={t("proxy.empty")} colSpan={7} />}
          </tbody>
        </table>
      </Card>

      <Card title={t("proxy.add_title")}>
        <form className="grid" onSubmit={create}>
          <label>{t("common.name")}<input required value={f.name} onChange={e => setF({ ...f, name: e.target.value })} placeholder="pool A" /></label>
          <label>{t("proxy.f_url")}<input required value={f.proxy_url} onChange={e => setF({ ...f, proxy_url: e.target.value })} placeholder="http://172.17.0.1:7897" /></label>
          <label>{t("proxy.f_credential_hint")}<input value={f.credential_hint} onChange={e => setF({ ...f, credential_hint: e.target.value })} placeholder="EROUTER_PROXY_A" /></label>
          <div className="tight"><button className="primary">{t("common.add")}</button></div>
        </form>
        <p className="tiny muted">{t("proxy.hint")}</p>
      </Card>
    </div>
  );
}
