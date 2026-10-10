// Katalog label (ADR-0000): teks operator TIDAK boleh di-hardcode di komponen.
// dashboard.py menyuntik window.__ENGRIX__ = {en:{...}, id:{...}}; saat dev,
// vite.config men-suntik dari web/strings/*.json lewat plugin devCatalog.
// Locale pilihan disimpan di localStorage, sama seperti sebelum migrasi React.

export type Catalog = Record<string, string>;
export type Catalogs = Record<string, Catalog>;

declare global {
  interface Window {
    __ENGRIX__?: Catalogs;
    __ENGRIX_LOCALE__?: string;
    __ENGRIX_BASE__?: string;
  }
}

const LOCALE_KEY = "engrix…cale";
export const FALLBACK_LOCALE = "en";

export function catalogs(): Catalogs {
  // Saat build/tsc, window.__ENGRIX__ diisi oleh host (dashboard.py / devCatalog
  // plugin). Kalau belum ada (unit test Node), katalog kosong: t() jatuh ke
  // kunci itu sendiri supaya hilang satu label gak bikin UI putih total.
  return (typeof window !== "undefined" && window.__ENGRIX__) || {};
}

export function currentLocale(): string {
  try {
    return localStorage.getItem(LOCALE_KEY) || window.__ENGRIX_LOCALE__ || FALLBACK_LOCALE;
  } catch {
    return window.__ENGRIX_LOCALE__ || FALLBACK_LOCALE;
  }
}

export function setLocale(next: string): void {
  try { localStorage.setItem(LOCALE_KEY, next); } catch { /* mode privat */ }
}

// t(): label dari katalog aktif, fallback katalog default, lalu kunci mentah.
export function t(key: string): string {
  const cs = catalogs();
  return cs[currentLocale()]?.[key] ?? cs[FALLBACK_LOCALE]?.[key] ?? key;
}

// fmt(): interpolasi {placeholder} -- sama persis dengan pola di katalog lama,
// jadi tidak perlu menulis ulang satu pun string terjemahan.
export function fmt(key: string, args: Record<string, string | number>): string {
  return t(key).replace(/\{(\w+)\}/g, (m, k) => (k in args ? String(args[k]) : m));
}

export function clientBase(): string {
  return (typeof window !== "undefined" && window.__ENGRIX_BASE__) || "";
}
