// Shell dashboard TASK-43: gate token -> sidebar bernavigasi (grup intent),
// topbar (status hidup, bahasa, tema, refresh, sign out), satu view aktif.
// Refresh = naikkan refreshKey, tiap view load() ulang (effect per view).

import { useCallback, useEffect, useState } from "react";
import { api, token, setToken } from "./api";
import { currentLocale, setLocale, t, fmt } from "./i18n";
import { OverviewView } from "./view_overview";
import { ProvidersView } from "./view_providers";
import { RequestsView } from "./view_requests";
import { KeysView, NodesView, ProxyView } from "./view_connect";
import { LogsView, SettingsView } from "./view_system";
import { ConfirmBar } from "./components";

type ViewId = "overview" | "requests" | "logs" | "providers" | "nodes" | "keys" | "proxy" | "settings";interface NavGroup { label: string; items: { id: ViewId; label: string }[] }

// Navigasi dikelompokkan berdasarkan maksud (Monitor / Connect / System):
// dulu 8 tab datar, operator bingung "mana connection mana quota".
function navGroups(): NavGroup[] {
  return [
    { label: t("nav.group.monitor"), items: [
      { id: "overview", label: t("nav.overview") },
      { id: "requests", label: t("nav.requests") },
      { id: "logs", label: t("nav.logs") },
    ] },
    { label: t("nav.group.connect"), items: [
      { id: "providers", label: t("nav.providers") },
      { id: "nodes", label: t("nav.nodes") },
      { id: "keys", label: t("nav.keys") },
      { id: "proxy", label: t("nav.proxy") },
    ] },
    { label: t("nav.group.system"), items: [
      { id: "settings", label: t("nav.settings") },
    ] },
  ];
}

interface Healthz { ok: boolean; uptime_s: number; upstream_frozen: boolean; version?: string }

export default function App() {
  const [view, setView] = useState<ViewId>(() => {
    const h = window.location.hash.replace("#", "");
    return (["overview", "requests", "logs", "providers", "nodes", "keys", "proxy", "settings"] as ViewId[]).includes(h as ViewId)
      ? (h as ViewId) : "overview";
  });
  const [authed, setAuthed] = useState<boolean>(() => Boolean(token()));
  const [tokenInput, setTokenInput] = useState("");
  const [gateErr, setGateErr] = useState<string | null>(null);
  const [healthz, setHealthz] = useState<Healthz | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [locale, setLocaleState] = useState(currentLocale());
  const [theme, setTheme] = useState<string>(() => {
    try { return localStorage.getItem("engrixrouter_theme") || "dark"; } catch { return "dark"; }
  });

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    document.documentElement.setAttribute("lang", locale);
    const fav = document.getElementById("favicon") as HTMLLinkElement | null;
    if (fav) fav.href = theme === "dark"
      ? "/static/assets/engrix-favicon-dark.svg"
      : "/static/assets/engrix-favicon-light.svg";
    try { localStorage.setItem("engrixrouter_theme", theme); } catch { /* mode privat */ }
  }, [theme, locale]);

  const ping = useCallback(async () => {
    try { setHealthz(await fetch("/health").then(r => r.json())); } catch { setHealthz(null); }
  }, []);
  useEffect(() => { ping(); const tm = setInterval(ping, 20000); return () => clearInterval(tm); }, [ping]);

  // Restart gateway dari dashboard: POST /api/system/restart (server menembak
  // helper detached yang stop+start proses), lalu /health dipoll sampai server
  // balik dengan boot baru -- semua view ke-refresh otomatis begitu hidup lagi
  // (refreshKey++), tanpa reload halaman. Timeout bukan gagal final: watchdog
  // di server tetap akan menyalakan lagi.
  const [restartState, setRestartState] = useState<"idle" | "confirm" | "sent" | "waiting">("idle");
  const [restartMsg, setRestartMsg] = useState<string | null>(null);
  const [restartErr, setRestartErr] = useState<string | null>(null);

  async function doRestart() {
    const baseline = healthz?.uptime_s ?? 0;
    setRestartState("sent"); setRestartMsg(t("system.restart_sending")); setRestartErr(null);
    try { await api("/api/system/restart", { method: "POST", body: "{}" }); }
    catch { /* respons bisa mati bersama server -- polling tetap lanjut */ }
    setRestartState("waiting"); setRestartMsg(t("system.restart_waiting"));
    const DOWN_DEADLINE = Date.now() + 20000;   // fase turun: server lama harus mati
    const UP_DEADLINE = Date.now() + 100000;    // fase naik: batas total
    let sawDown = false;
    for (;;) {
      if (Date.now() > UP_DEADLINE) {
        setRestartState("idle"); setRestartMsg(null);
        setRestartErr(fmt("system.restart_timeout", { value: 100 }));
        return;
      }
      await new Promise(r => setTimeout(r, 1000));
      let ok = false; let uptime = 0;
      try {
        const h = await fetch("/health").then(r => r.json());
        ok = Boolean(h?.ok); uptime = Number(h?.uptime_s ?? 0);
      } catch { /* masih mati */ }
      if (!ok) { sawDown = true; continue; }
      if (sawDown || uptime < baseline - 5) {
        setRestartState("idle"); setRestartErr(null);
        setRestartMsg(t("system.restart_done"));
        setTimeout(() => setRestartMsg(null), 8000);
        setRefreshKey(k => k + 1);
        ping();
        return;
      }
      if (Date.now() > DOWN_DEADLINE) {
        setRestartState("idle"); setRestartMsg(null);
        setRestartErr(t("system.restart_failed"));
        return;
      }
    }
  }

  // 401 dari endpoint mana pun = token mati/salah: turun ke gate, bukan
  // cuma menampilkan error di dalam view.
  useEffect(() => {
    function onErr(e: Event) {
      const detail = (e as CustomEvent<{ status?: number }>).detail;
      if (detail?.status === 401 && token()) { setAuthed(false); }
    }
    window.addEventListener("engrix:api-status", onErr);
    return () => window.removeEventListener("engrix:api-status", onErr);
  }, []);

  // Brand yang sedang difokus (TASK-46): klik merek di list providers ->
  // halaman merek itu sendiri (#providers/<brand>), bukan accordion di
  // satu halaman panjang. Nol fokus = list merek.
  const [focusBrand, setFocusBrand] = useState<string>(() => {
    const h = window.location.hash.replace("#", "");
    return h.startsWith("providers/") ? decodeURIComponent(h.slice("providers/".length)) : "";
  });

  function show(v: ViewId) {
    setView(v);
    setFocusBrand("");
    window.location.hash = v;
  }

  function openBrand(brand: string) {
    setView("providers");
    setFocusBrand(brand);
    window.location.hash = "providers/" + encodeURIComponent(brand);
  }

  // Back/forward antar tab = hash berubah tanpa reload; tanpa listener ini
  // tombol browser bikin URL dan konten tidak nyambung lagi.
  useEffect(() => {
    function onHash() {
      const h = window.location.hash.replace("#", "");
      if (h.startsWith("providers/")) {
        setView("providers");
        setFocusBrand(decodeURIComponent(h.slice("providers/".length)));
        return;
      }
      if ((["overview", "requests", "logs", "providers", "nodes", "keys", "proxy", "settings"] as string[]).includes(h)) {
        setView(h as ViewId);
        setFocusBrand("");
      }
    }
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  if (!authed) {
    return (
      <div className="gate">
        <img className="gate-logo theme-dark" src="/static/assets/engrix-lockup-router-dark-animated.svg" alt="engrix router" />
        <img className="gate-logo theme-light" src="/static/assets/engrix-lockup-router-light-animated.svg" alt="" />
        <img className="mascot theme-dark" src="/static/assets/engrix-mascot-standby-dark.svg" alt="" />
        <img className="mascot theme-light" src="/static/assets/engrix-mascot-standby-light.svg" alt="" />
        <h2>{t("gate.title")}</h2>
        <p className="muted tiny">
          {fmt("gate.hint", { value: "EROUTER_ADMIN_TOKEN" })}
        </p>
        {gateErr && <p className="tiny err-text">{gateErr}</p>}
        <form
          className="row tight"
          onSubmit={async (ev) => {
            ev.preventDefault();
            setToken(tokenInput.trim());
            try {
              await api<Healthz[]>("/api/connections");
              setAuthed(true); setGateErr(null); setTokenInput("");
            } catch (e) {
              setToken("");
              setGateErr(t("gate.bad_token") + " · " + (e as Error).message);
            }
          }}
        >
          <label>{t("gate.token_label")}
            <input type="password" value={tokenInput} onChange={e => setTokenInput(e.target.value)} placeholder={t("gate.token_ph")} autoFocus />
          </label>
          <button className="primary">{t("gate.sign_in")}</button>
        </form>
        <LocaleThemeBar locale={locale} setLocale={l => { setLocale(l); setLocaleState(l); }} theme={theme} setTheme={setTheme} />
      </div>
    );
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          {/* Lockup wordmark resmi (SVG pixel-font, adaptif tema) -- bukan
              teks telanjang: brand harus kebaca sebagai produk, bukan nama
              proses. img.theme-* display-nya diatur engrix.css per tema. */}
          <img className="logo theme-dark" src="/static/assets/engrix-lockup-router-dark-animated.svg" alt="engrix router" />
          <img className="logo theme-light" src="/static/assets/engrix-lockup-router-light-animated.svg" alt="" />
          <span className="ver">{healthz?.version ? `v${healthz.version}` : ""}</span>
          <span className="byline">{t("ui.byline")}</span>
        </div>
        {navGroups().map(g => (
          <div key={g.label}>
            <div className="nav-group">{g.label}</div>
            {g.items.map(it => (
              <button
                key={it.id}
                className="tab"
                aria-selected={view === it.id}
                onClick={() => { show(it.id); setRefreshKey(k => k + 1); }}
              >{it.label}</button>
            ))}
          </div>
        ))}
        <div className="sidebar-foot">
          <LocaleThemeBar locale={locale} setLocale={l => { setLocale(l); setLocaleState(l); }} theme={theme} setTheme={setTheme} />
        </div>
      </aside>

      <main>
        <div className="row status-row">
          <span className="tiny" style={{ color: healthz?.upstream_frozen ? "var(--danger)" : "var(--ok)" }}>
            {healthz
              ? fmt("status.alive", { value: String(Math.round(healthz.uptime_s)) }) + (healthz.upstream_frozen ? t("status.frozen") : "")
              : t("status.problem")}
          </span>
          <span className="grow" />
          <button className="tight tiny" onClick={() => { setRefreshKey(k => k + 1); }} title={t("top.refresh")}>{t("top.refresh")}</button>
          {restartState === "confirm" ? (
            <ConfirmBar text={t("system.restart_confirm")} onYes={doRestart} onNo={() => setRestartState("idle")} />
          ) : (
            <button className="tight tiny" disabled={restartState !== "idle"} title={t("system.restart_confirm")} onClick={() => setRestartState("confirm")}>{t("top.restart")}</button>
          )}
          <button className="tight tiny" onClick={() => { setToken(""); setAuthed(false); }}>{t("top.signout")}</button>
          {restartMsg && <span className="tiny muted">{restartMsg}</span>}
          {restartErr && <span className="tiny err-text">{restartErr}</span>}
        </div>

        {view === "overview" && <OverviewView refreshKey={refreshKey} />}
        {view === "providers" && <ProvidersView focusBrand={focusBrand} onOpenBrand={openBrand} onBack={() => show("providers")} />}
        {view === "requests" && <RequestsView refreshKey={refreshKey} />}
        {view === "nodes" && <NodesView refreshKey={refreshKey} />}
        {view === "keys" && <KeysView refreshKey={refreshKey} />}
        {view === "proxy" && <ProxyView refreshKey={refreshKey} />}
        {view === "settings" && <SettingsView refreshKey={refreshKey} />}
        {view === "logs" && <LogsView refreshKey={refreshKey} />}
      </main>
    </div>
  );
}

function LocaleThemeBar(props: { locale: string; setLocale: (l: string) => void; theme: string; setTheme: (t: string) => void }) {
  return (
    <div className="row tight">
      <button className={"tiny" + (props.locale === "en" ? " primary" : "")} title={t("ui.english")} aria-label={t("ui.english")} onClick={() => props.setLocale("en")}>EN</button>
      <button className={"tiny" + (props.locale === "id" ? " primary" : "")} title={t("ui.indonesian")} aria-label={t("ui.indonesian")} onClick={() => props.setLocale("id")}>ID</button>
      <button className="tiny" onClick={() => props.setTheme(props.theme === "dark" ? "light" : "dark")}>{t("top.theme")}</button>
    </div>
  );
}
