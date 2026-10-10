// Komponen kartu universal (TASK-43): SATU bentuk untuk semua section supaya
// operator membaca layout yang sama di layar mana pun. Aturan disiplin:
// - tiap angka tampil dengan label + unit + konteks waktu, tanpa field mentah;
// - status枚举 dirender badge dengan warna bermakna (legend ada di katalog);
// - state kosong = EmptyState bermaskot, bukan tabel hampa.

import { type ReactNode, useEffect, useState } from "react";
import { fmt, t } from "./i18n";
import { dcount, num, pct1, pctBand, short } from "./format";
import type { QuotaReading } from "./types";

export function Card(props: { title: ReactNode; tools?: ReactNode; children: ReactNode; tone?: "danger"; inner?: boolean }) {
  return (
    <section className={"card" + (props.tone === "danger" ? " card-danger" : "") + (props.inner ? " prov-card" : "")}>
      <header className="card-head">
        <h3>{props.title}</h3>
        {props.tools && <div className="row tight">{props.tools}</div>}
      </header>
      {props.children}
    </section>
  );
}

// Badge status -- kelas warna dipetakan dari enum backend (sama dengan peta
// badge dashboard lama; enum = data vendor, label teknisnya sengaja English).
const BADGE_CLASS: Record<string, string> = {
  ok: "ok", active: "ok", unknown: "info", cooling: "warn", unavailable: "err",
  needs_reauth: "err", rate_limited: "warn", upstream_error: "err", client_error: "warn",
  budget_exceeded: "warn", aborted: "info", locked: "warn", in_flight: "violet", dry_run: "violet",
};

export function Badge(props: { status: string | null | undefined }) {
  const s = props.status || "unknown";
  return <span className={"badge " + (BADGE_CLASS[s] || "info")}>{s}</span>;
}

// State kosong selalu bermaskot (aturan pack: maskot hidup di dalam produk);
// teksnya dari katalog, bukan literal komponen.
export function EmptyState(props: { label: string; colSpan?: number }) {
  const body = (
    <div className="empty-state">
      <img className="theme-dark" src="/static/assets/engrix-mascot-default-dark.svg" alt="" />
      <img className="theme-light" src="/static/assets/engrix-mascot-default-light.svg" alt="" />
      <span>{props.label}</span>
    </div>
  );
  if (props.colSpan !== undefined) {
    return <tr><td colSpan={props.colSpan}>{body}</td></tr>;
  }
  return body;
}

export function Tile(props: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="tile">
      <div className="tiny muted">{props.label}</div>
      <div className="kpi">{props.value}</div>
      {props.note && <div className="tiny muted">{props.note}</div>}
    </div>
  );
}

// Satu baris kuota, format kartu contoh operator: dot band + scope +
// "used / total" + persen sisa + hitung-mundur sampai (reset|expire) — semua
// unit eksplisit, tidak ada angka telanjang.
export function QuotaRow(props: { reading: QuotaReading; expire?: boolean }) {
  const r = props.reading;
  if (r.unlimited) {
    return (
      <div className="qrow">
        <span className="qname">{r.scope || t("common.scope")}</span>
        <span className="qbar" />
        <span className="qnum na">{t("quota.unlimited")}</span>
      </div>
    );
  }
  const band = pctBand(r.remaining_pct);
  const until = r.reset_at ?? null;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (until === null) return;
    const timer = setInterval(() => setNow(Date.now()), 30000);
    return () => clearInterval(timer);
  }, [until]);
  return (
    <div className="qrow">
      <span className={"dot " + band} title={t("quota.legend")} aria-hidden="true" />
      <span className="qname">{r.scope || t("common.scope")}</span>
      <span className="qbar"><i className={band} style={{ width: Math.max(0, Math.min(100, r.remaining_pct ?? 0)) + "%" }} /></span>
      <span className={"qnum " + band}>
        <b>{num(r.used ?? null)} / {num(r.total ?? null)}</b>{" · "}
        {fmt("quota.pct_left", { value: pct1(r.remaining_pct) })}
      </span>
      {until !== null && (
        <span className="quntil tiny muted">
          {fmt(props.expire ? "quota.expires_in" : "quota.resets_in", { value: dcount(until, now) })}
          {" · "}{t("quota.until")} {new Date(until).toLocaleTimeString("en-GB", { hour12: false })}
        </span>
      )}
    </div>
  );
}

// Grup reading satu koneksi + hitungannya (contoh user: "2 quotas").
export function QuotaGroup(props: { readings: QuotaReading[] }) {
  if (!props.readings.length) {
    return <p className="tiny muted">{t("providers.no_quota")}</p>;
  }
  return (
    <>
      <p className="tiny muted">{fmt("providers.quota_count", { value: props.readings.length })}</p>
      {props.readings.map((r, i) => <QuotaRow key={r.scope + i} reading={r} />)}
    </>
  );
}

// Label kecil: kunci katalog + nilai, untuk baris meta yang selama ini raw.
export function Meta(props: { k: string; v: ReactNode }) {
  return <span className="tiny muted"><span className="metalabel">{t(props.k)}</span> {props.v}</span>;
}

// Hasil aksi in-place di tombol (Test → "ok 340ms" → balik) tanpa alert:
// modal alert pernah jadi satu-satunya feedback di form; TASK-43 ubah inline.
export function useActionFeedback(): [string | null, (s: string | null) => void] {
  const [msg, setMsg] = useState<string | null>(null);
  useEffect(() => {
    if (!msg) return;
    const timer = setTimeout(() => setMsg(null), 8000);
    return () => clearTimeout(timer);
  }, [msg]);
  return [msg, setMsg];
}

export function InlineMsg(props: { msg: string | null; kind?: "ok" | "err" }) {
  if (!props.msg) return null;
  return <p className={"tiny inline-msg " + (props.kind === "err" ? "err" : "ok")}>{props.msg}</p>;
}

// Konfirmasi hapus: bukan window.confirm tapi satu baris dua tombol (jangan
// klik dua kali tanpa tau apa yang hilang -- teks confirm dari katalog).
export function ConfirmBar(props: { text: string; onYes: () => void; onNo: () => void }) {
  return (
    <span className="row tight confirm-bar">
      <span className="tiny">{props.text}</span>
      <button className="tiny danger" onClick={props.onYes}>{t("common.yes")}</button>
      <button className="tiny" onClick={props.onNo}>{t("common.no")}</button>
    </span>
  );
}

export function IdLink(props: { id: string }) {
  return <code className="mono tiny" title={props.id}>{short(props.id)}</code>;
}
