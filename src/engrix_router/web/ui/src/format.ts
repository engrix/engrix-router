// Formatter = satu-satunya tempat yang boleh mengubah angka jadi teks. Aturan
// TASK-43: tidak ada data mentah / ambigu di layar operator -- setiap nilai
// punya format eksplisit, unit, dan konteks waktu.

export type LocaleTag = "en" | "id";

// locale aktif untuk Intl: label "ID" di UI berarti katalog id.json, jadi
// pemformatan angka juga ikut (ribuan pakai titik vs koma dibedakan Intl).
export function num(n: number | null | undefined, locale?: LocaleTag): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat(locale || "en", { maximumFractionDigits: 2 }).format(n);
}

// Uang vendor: selalu USD, dua sampai enam desimal signifikan -- gateway tidak
// pernah menebak harga, angka 0 harus terbaca "$0" bukan hilang.
export function money(n: number | null | undefined, locale?: LocaleTag): string {
  if (n === null || n === undefined) return "—";
  const v = new Intl.NumberFormat(locale || "en", {
    style: "currency", currency: "USD",
    minimumFractionDigits: 2, maximumFractionDigits: n > 0 && n < 0.01 ? 6 : 2,
  }).format(n);
  return v;
}

// stempel epoch-ms -> jam lokal operator (bukan UTC mentah) supaya "kapan"
// terjawab tanpa perlu jadi jagoan epoch.
export function clock(ms: number | string | null | undefined, locale?: LocaleTag): string {
  if (ms === null || ms === undefined || ms === "") return "—";
  const d = typeof ms === "number" ? new Date(ms) : new Date(String(ms));
  if (Number.isNaN(d.getTime())) return String(ms);
  return d.toLocaleString(locale === "id" ? "id-ID" : "en-GB", { hour12: false });
}

// Countdown "in 21d 12h 3m" (contoh user): dipangkas dari unit terbesar supaya
// tetap satu baris; nilai negatif = waktu terlewati, ditampilkan "now".
// Menerima epoch-ms ATAU string ISO: backend menyimpan kedua bentuk itu
// (expires_at koneksi = ISO, reset_at kuota = ms) -- dulu string masuk apa
// adanya ke aritmetika dan keluar "NaNd NaNh NaNm" di layar operator.
export function dcount(ms: number | string | null | undefined, nowMs = Date.now()): string {
  if (ms === null || ms === undefined || ms === "") return "—";
  const epoch = typeof ms === "number" ? ms : new Date(ms).getTime();
  if (!Number.isFinite(epoch)) return "—";
  let s = Math.max(0, Math.round((epoch - nowMs) / 1000));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60); if (m < 60) return m + "m";
  s = Math.floor(s / 60);
  const h = Math.floor(s / 60); if (h < 24) return h + "h " + (m % 60) + "m";
  const d = Math.floor(h / 24);
  return d + "d " + (h % 24) + "h " + (m % 60) + "m";
}

// Band aman kuota: thresholds diproklamasikan di katalog (ui.band_*) supaya
// operator tahu ARTI warna, bukan nebak dari rambu lalu lintas.
export function pctBand(p: number | null | undefined): "ok" | "mid" | "low" | "na" {
  if (p === null || p === undefined) return "na";
  if (p >= 50) return "ok";
  if (p >= 20) return "mid";
  return "low";
}

// Persen dengan satu desimal kalau perlu (99.32 bukan "99.319999"): sumber
// angka sudah bulat per-satuan, pembulatan tampil = tanggung jawab sini.
export function pct1(p: number | null | undefined): string {
  if (p === null || p === undefined) return "—";
  // clamp 0..100: vendor bisa kasih -0.001% karena pembulatan float, dan
  // "-0% left" di layar operator itu ambigu -- nol ya nol.
  const v = Math.min(100, Math.max(0, Math.round(p * 10) / 10));
  return (v === 0 ? "0" : v.toFixed(1).replace(/\.0$/, "")) + "%";
}

// Escape HTML TIDAK diperlukan di React (auto per-node); helper ini untuk
// string payload teknis yang disisipin ke innerHTML pre? Tidak dipakai --
// payload trace dirender sebagai text node, React yang mengescape.

// potongan id ULID buat label kolom (kolom sempit, id 26 karakter meleleh)
export function short(id: string | null | undefined, n = 8): string {
  if (!id) return "—";
  return id.slice(0, n);
}

// "sisa / total" gaya kartu contoh user: "2.71 / 250" -- dipakai, titik nol
// desimal dibuang (0 / 100, bukan 0.00 / 100).
export function usedTotal(used: number | null, total: number | null, locale?: LocaleTag): string {
  return num(used, locale) + " / " + num(total, locale);
}
