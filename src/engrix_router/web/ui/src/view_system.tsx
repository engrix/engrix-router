// Settings + Logs (TASK-43). Settings = registry runtime dari backend
// (values+schema); tiap baris punya doc + default + tipe, input divalidasi
// JSON lalu dikirim sebagai patch diff-only (key yang berubah saja).
// Logs = streaming SSE /api/logs/stream; parser dipakai bersama (init + line).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, stream, token } from "./api";
import { fmt, t } from "./i18n";
import { Badge, Card, InlineMsg } from "./components";
import type { SettingsDoc } from "./types";

export function SettingsView({ refreshKey }: { refreshKey: number }) {
  const [doc, setDoc] = useState<SettingsDoc | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [q, setQ] = useState("");

  const load = useCallback(async () => {
    try {
      const s = await api<SettingsDoc>("/api/settings");
      setDoc(s);
      const d: Record<string, string> = {};
      for (const [k, v] of Object.entries(s.values)) d[k] = JSON.stringify(v);
      setDraft(d);
      setErr(null);
    } catch (e) { setErr((e as ApiError).message); }
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);

  const keys = useMemo(() => {
    if (!doc) return [];
    return Object.keys(doc.schema)
      .filter(k => !q || k.includes(q.toLowerCase()))
      .sort();
  }, [doc, q]);

  // Rak per kategori (TASK-45): satu tabel flat 50 baris itu bukan config
  // UI, itu dump. Namespace kunci = kategorinya (budget.*, health.*, ...),
  // knob provider (zcode.*, qoder.*) otomatis jadi rak sendiri karena
  // register_defaults mewajibkan prefix id provider. Provider baru = rak
  // baru, tanpa perlu sentuh kode UI.
  const groups = useMemo(() => {
    const map: Record<string, string[]> = {};
    for (const k of keys) {
      const g = doc?.schema[k]?.group || k.split(".", 1)[0];
      (map[g] = map[g] || []).push(k);
    }
    return map;
  }, [doc, keys]);
  const CORE_ORDER = ["routing", "health", "quota", "budget", "ratelimit",
    "observability", "nodes", "catalog", "client", "pricing", "services"];
  const groupOrder = useMemo(() => {
    const inCore = Object.keys(groups).filter(g => CORE_ORDER.includes(g))
      .sort((a, b) => CORE_ORDER.indexOf(a) - CORE_ORDER.indexOf(b));
    const rest = Object.keys(groups).filter(g => !CORE_ORDER.includes(g)).sort();
    return [...inCore, ...rest];
  }, [groups]);
  const [openGroups, setOpenGroups] = useState<Record<string, boolean>>({});
  // Search = expand semua grup yang match, biar hasil gak tersembunyi collapse.
  useEffect(() => {
    if (q) setOpenGroups(Object.fromEntries(groupOrder.map(g => [g, !!groups[g]?.length])));
  }, [q]); // eslint-disable-line react-hooks/exhaustive-deps

  function groupLabel(g: string): string {
    const key = `settings.group.${g}`;
    const label = t(key);
    // t() jatuh ke kunci mentah kalau belum ada labelnya -- untuk grup
    // provider baru pakai id-nya langsung, tetap terbaca manusia.
    return label === key ? g : label;
  }

  const pending = useMemo(() => {
    if (!doc) return [];
    return Object.keys(draft).filter(k => {
      if (!(k in doc.schema)) return false;
      try { return draft[k] !== JSON.stringify(doc.values[k]); } catch { return false; }
    });
  }, [doc, draft]);

  async function saveAll() {
    if (!doc) return;
    const patch: Record<string, unknown> = {};
    for (const k of pending) {
      try { patch[k] = JSON.parse(draft[k]); }
      catch { setErr(fmt("settings.invalid_json", { value: k })); return; }
    }
    if (!Object.keys(patch).length) { setOk(t("settings.nothing_changed")); return; }
    setErr(null);
    try {
      await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) });
      setOk(fmt("settings.saved", { value: Object.keys(patch).length }));
      load();
    } catch (e) { setErr((e as ApiError).message); }
  }

  return (
    <Card
      title={t("settings.title")}
      tools={<>
        <input className="tiny-input" placeholder={t("settings.search")} value={q} onChange={e => setQ(e.target.value)} size={18} />
        <button className="primary tiny" disabled={!pending.length} onClick={saveAll}>{t("settings.save_all")}</button>
      </>}
    >
      <InlineMsg msg={err} kind="err" />
      <InlineMsg msg={ok} kind="ok" />
      {pending.length > 0 && <p className="tiny muted">{fmt("settings.pending", { value: pending.length })}</p>}
      {!keys.length && <p className="muted tiny">{t("settings.no_match")}</p>}
      {groupOrder.map(g => {
        const rows = groups[g] || [];
        if (!rows.length) return null;
        const open = !!openGroups[g] || !!q;
        const pendingHere = rows.filter(k => pending.includes(k)).length;
        return (
          <div key={g} className={"settings-group" + (open ? " open" : "")}>
            <header
              className="settings-group-head"
              onClick={() => setOpenGroups(v => ({ ...v, [g]: !open }))}
            >
              <span className="brand-caret">{open ? "▾" : "▸"}</span>
              <span className="settings-group-name">{groupLabel(g)}</span>
              <span className="badge" title={t("settings.col_key")}>{rows.length}</span>
              {pendingHere > 0 && <span className="badge warn">{fmt("settings.pending", { value: pendingHere })}</span>}
            </header>
            {open && (
              <table>
                <thead><tr>
                  <th>{t("settings.col_key")}</th><th>{t("settings.col_value")}</th>
                  <th>{t("settings.col_default")}</th><th>{t("settings.col_type")}</th><th>{t("settings.col_note")}</th>
                </tr></thead>
                <tbody>
                  {rows.map(k => {
                    const schema = doc!.schema[k];
                    const changed = pending.includes(k);
                    return (
                      <tr key={k} className={changed ? "row-changed" : undefined}>
                        <td className="mono tiny">{k}</td>
                        <td>
                          <input
                            className="tiny-input mono"
                            value={draft[k] ?? JSON.stringify(doc!.values[k] ?? schema.default)}
                            onChange={e => setDraft({ ...draft, [k]: e.target.value })}
                            aria-label={k}
                          />
                        </td>
                        <td className="tiny muted mono">{JSON.stringify(schema.default)}</td>
                        <td className="tiny">{schema.type}</td>
                        <td className="tiny muted">{schema.doc || fmt("settings.doc", { value: k })}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </div>
        );
      })}
    </Card>
  );
}

interface LogLine { ts: number; level: string; ns: string; line: string }

// Satu parser untuk init batch + delta: EventSource tidak bisa bawa header
// authorization, jadi stream() manual + JSON framing sendiri.
export function LogsView({ refreshKey }: { refreshKey: number }) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [status, setStatus] = useState<"idle" | "running" | "done" | "error">("idle");
  const [err, setErr] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const boxRef = useRef<HTMLPreElement | null>(null);

  const follow = useCallback(async () => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    setLines([]); setStatus("running"); setErr(null);
    try {
      await stream("/api/logs/stream?seconds=60", (payload) => {
        try {
          const j = JSON.parse(payload);
          if (j.type === "init") setLines((j.lines || []) as LogLine[]);
          else if (j.line) setLines(prev => [...prev.slice(-4000), j.line as LogLine]);
        } catch { /* baris bukan-JSON diabaikan sadar-sadaran */ }
      }, ac.signal);
      setStatus("done");
    } catch (e) {
      if ((e as Error).name === "AbortError") setStatus("idle");
      else { setStatus("error"); setErr((e as ApiError).message); }
    }
  }, []);

  // Buka tab = langsung tail (realtime, bukan tombol dulu). Stream vendor-nya
  // berdurasi 60 s; begitu selesai badge jadi "active" (selesai normal), bukan
  // "aborted" -- dulu semua state non-running dipetakan ke aborted sehingga
  // "belum mulai" dan "selesai" terlihat sama dengan "dibatalkan".
  useEffect(() => {
    follow();
    return () => abortRef.current?.abort();
  }, [follow, refreshKey]);
  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => { if (boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight; }, [lines]);

  const BADGE: Record<string, string> = {
    idle: "unknown", running: "in_flight", done: "active", error: "upstream_error",
  };

  return (
    <Card
      title={t("log.title")}
      tools={<>
        <Badge status={BADGE[status]} />
        <button className="tiny primary" onClick={follow} disabled={!token() || status === "running"}>{t("log.follow")}</button>
        <button className="tiny" onClick={() => { abortRef.current?.abort(); setLines([]); setStatus("idle"); }}>{t("log.clear")}</button>
      </>}
    >
      <InlineMsg msg={err} kind="err" />
      <pre className="code log-box" ref={boxRef}>
        {lines.length === 0
          ? (status === "running" ? t("log.waiting") : status === "idle" ? t("log.idle") : "")
          : lines.map(l => l.line).join("\n")}
        {status === "done" ? "\n" + t("log.done") : ""}
      </pre>
      <p className="tiny muted">{fmt("log.count", { value: lines.length })}</p>
    </Card>
  );
}
