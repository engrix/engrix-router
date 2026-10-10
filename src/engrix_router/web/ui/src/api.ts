// Client API + state token. Semua request admin lewat api() supaya satu tempat
// untuk header authorization, parsing error vendor ({error:{message}}), dan
// pelepasan gate 401 (token salah = layar gate muncul lagi).

const TOKEN_KEY = "engri…oken";

export function token(): string {
  try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; }
}

export function setToken(v: string): void {
  try { v ? localStorage.setItem(TOKEN_KEY, v) : localStorage.removeItem(TOKEN_KEY); } catch { /* mode privat */ }
}

export class ApiError extends Error {
  status: number;
  body: unknown;
  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

export async function api<T>(path: string, opts: RequestInit & { body?: string } = {}): Promise<T> {
  const headers: Record<string, string> = {
    authorization: "Bearer " + token(),
    "content-type": "application/json",
    ...(opts.headers as Record<string, string> | undefined),
  };
  const res = await fetch(path, { ...opts, headers });
  const text = await res.text();
  let body: unknown = null;
  try { body = JSON.parse(text); } catch { body = { raw: text }; }
  if (!res.ok) {
    const b = body as { error?: { message?: string }; detail?: string };
    // 401 diberitakan global: App menurunkan gate login tanpa tiap view harus
    // menangani token mati sendiri-sendiri (satu keputusan, satu pemilik).
    if (res.status === 401) {
      window.dispatchEvent(new CustomEvent("engrix:api-status", { detail: { status: 401 } }));
    }
    throw new ApiError(b?.error?.message || b?.detail || res.statusText, res.status, body);
  }
  return body as T;
}

// Snapshot quota dibagikan antar view (Overview & Providers) supaya kedua tab
// menampilkan angka yang identik dan TTL cache vendor tidak di-bypass dua kali.
// force=true untuk refresh manual; TTL 25 s ditekuni di client, 30 s di server.
let _quotaSnap: Promise<unknown> | null = null;
export function quotaSnapshot(force = false): Promise<import("./types").QuotaSnapshot> {
  if (force) _quotaSnap = null;
  if (!_quotaSnap) {
    _quotaSnap = api<import("./types").QuotaSnapshot>("/api/quota")
      .catch((e: ApiError) => ({ providers: [], error: e.message }))
      .then((q) => { setTimeout(() => { _quotaSnap = null; }, 25000); return q; });
  }
  return _quotaSnap as Promise<import("./types").QuotaSnapshot>;
}

// SSE manual lewat fetch+reader (bukan EventSource) karena EventSource tidak
// bisa kirim header authorization.
export async function stream(
  path: string,
  onData: (payload: string) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(path, { headers: { authorization: "Bearer " + token() }, signal });
  if (!res.ok || !res.body) throw new ApiError(res.statusText, res.status, null);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      if (part.startsWith("data:")) onData(part.slice(5));
    }
  }
}
