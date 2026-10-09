-- engrix-router :: skema SQLite (satu-satunya sumber DDL -- ubah di sini saja)
-- Versi skema: 2  (naik versi WAJIB kèm entri MIGRASI di storage/sqlite.py; kolom baru
--                  di file ini cuma kepake buat DB yang baru dibuat, DB lama di-migrate)
--
-- Dasar: skema 9router v0.5.95 (dibaca live dari DB produksi owner;
-- dump mentahnya disimpan di luar repo publik),
-- dengan 4 deviasi yang disengaja. Tiap deviasi dikomentari di tempatnya:
--
--   D1  health/keys/lock DIPISAH dari blob kredensial.
--       9router nyimpen testStatus/errorCode/backoffLevel/lastUsedAt/consecutiveUseCount
--       dan modelLock_<model> di dalam JSON providerConnections.data
--       (src/sse/services/auth.js:242-340). Efeknya: nulis lock = nulis ulang token,
--       audit reset susah, dan gak bisa query "koneksi mana yang lagi locked".
--   D2  api_keys disimpan sebagai HASH, bukan plaintext.
--       9router: `key TEXT UNIQUE` + `SELECT isActive FROM apiKeys WHERE key = ?`
--       (src/lib/db/repos/apiKeysRepo.js:70-74) dan GET /api/keys ngembaliin semua key utuh.
--   D3  kegagalan request IKUT dicatat di tabel pemakaian.
--       9router cuma nulis usageHistory saat sukses (handlers/chatCore.js:400,475) --
--       di DB owner: 35.453 baris, status != 'ok' = 0 baris. Jadi view "pemakaian"
--       gak pernah nunjukin error.
--   D4  satu request_id (ULID-ish) jadi join key lintas trace/stage/log.
--       9router gak punya korelasi: stream pakai id acak (chatCore/streamingHandler.js:121)
--       dan usage baris terpisah, join-nya cuma (provider, model, connectionId, ts≈).
--
-- Konvensi: TEXT ISO-8601 UTC untuk kolom *_at, INTEGER epoch-ms untuk kolom ts
-- (ts dipakai buat sorting/range query, jadi index-nya bekerja).

PRAGMA foreign_keys = ON;

-- ═══════════════════════ identitas & kredensial ═══════════════════════

-- Kunci API buat KLIEN yang manggil gateway ini (bukan kunci provider).
-- Nama kolom disengaja key_hash + key_prefix: plaintext-nya cuma sekali keliatan
-- saat dibuat, setelah itu gak pernah ke-disk.
CREATE TABLE IF NOT EXISTS api_keys (
  id            TEXT PRIMARY KEY,
  name          TEXT NOT NULL,
  key_hash      TEXT NOT NULL UNIQUE,
  key_prefix    TEXT NOT NULL,
  machine_id    TEXT,
  is_active     INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL,
  last_used_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);

-- Provider yang bisa ditambahkan user lewat API tanpa nulis kode:
-- upstream OpenAI-compatible / Anthropic-compatible / embedding endpoint.
-- Padanan 9router: tabel providerNodes (src/lib/db/repos/nodesRepo.js:55-70).
-- Bedanya: prefix di sini UNIQUE dan divalidasi, karena tabrakan prefix adalah
-- bug routing yang senyap (parseModel split di '/' pertama, services/model.js:34-46).
CREATE TABLE IF NOT EXISTS nodes (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL UNIQUE,
  prefix       TEXT NOT NULL UNIQUE,
  type         TEXT NOT NULL,               -- openai-compatible | anthropic-compatible | custom-embedding
  api_type     TEXT NOT NULL DEFAULT 'chat',-- chat | responses | embeddings
  base_url     TEXT NOT NULL,
  is_active    INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_prefix ON nodes(prefix);

-- Satu baris = satu akun/kredensial upstream, milik satu provider (built-in) atau node.
-- cred_json: rahasia upstream (api_key / access_token / refresh_token / expires_at).
-- psd_json:  data non-rahasia yang dibutuhkan executor (user_id, machine_id, org, region).
-- Padanan 9router: providerConnections (schema live + connectionsRepo.js:52-78).
CREATE TABLE IF NOT EXISTS connections (
  id             TEXT PRIMARY KEY,
  provider       TEXT NOT NULL,            -- id registry, atau node id utk custom node
  node_id        TEXT REFERENCES nodes(id) ON DELETE CASCADE,
  auth_type      TEXT NOT NULL,            -- apikey | oauth | none
  name           TEXT,
  email          TEXT,
  priority       INTEGER NOT NULL DEFAULT 100,
  is_active      INTEGER NOT NULL DEFAULT 1,
  proxy_pool_id  TEXT REFERENCES proxy_pools(id) ON DELETE SET NULL,
  cred_json      TEXT NOT NULL,
  psd_json       TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conn_lookup ON connections(provider, is_active, priority);

-- D1: kesehatan koneksi, tabel terpisah, 1:1 dengan connections.
-- test_status: unknown | active | cooling | unavailable | needs_reauth
-- backoff_level dipakai buat naikkan jeda rate-limit secara eksponensial.
CREATE TABLE IF NOT EXISTS connection_health (
  connection_id          TEXT PRIMARY KEY REFERENCES connections(id) ON DELETE CASCADE,
  test_status            TEXT NOT NULL DEFAULT 'unknown',
  error_code             TEXT,
  error_class            TEXT,
  last_error             TEXT,
  last_error_at          INTEGER,
  backoff_level          INTEGER NOT NULL DEFAULT 0,
  rate_limited_until     INTEGER,
  last_used_at           INTEGER,
  consecutive_use_count  INTEGER NOT NULL DEFAULT 0,
  last_probe_at          INTEGER,
  updated_at             INTEGER NOT NULL
);

-- D1: lock per model (sentinel '*' = lock level akun). Di 9router ini nyampur
-- ke blob kredensial sebagai kunci modelLock_qfmodel dst.
CREATE TABLE IF NOT EXISTS model_locks (
  id             INTEGER PRIMARY KEY,
  connection_id  TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  model          TEXT NOT NULL,             -- '*' = semua model di koneksi ini
  locked_until   INTEGER NOT NULL,           -- epoch-ms
  reason         TEXT,
  created_at     INTEGER NOT NULL,
  UNIQUE(connection_id, model)
);
CREATE INDEX IF NOT EXISTS idx_locks_live ON model_locks(connection_id, locked_until);

-- Salinan persis katalog vendor (endpoint list model yang gak didokumentasikan), satu baris per
-- model per akun. Kenapa di DB bukan hardcode: config model yang kita karang sendiri
-- TIDAK ditolak vendor -- dia diterima dan diem-diam jalan di model yang lebih murah
-- (silent downgrade, terukur langsung). Jadi satu-satunya config yang aman adalah
-- yang dibaca dari vendor, dan itu per-akun (enable/price_factor ikut plan).
CREATE TABLE IF NOT EXISTS provider_catalog (
  provider       TEXT NOT NULL,
  connection_id  TEXT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  model_key      TEXT NOT NULL,
  config_json    TEXT NOT NULL,
  catalog_hash   TEXT NOT NULL,
  fetched_at     INTEGER NOT NULL,
  PRIMARY KEY(provider, connection_id, model_key)
);
CREATE INDEX IF NOT EXISTS idx_catalog_model ON provider_catalog(provider, model_key, fetched_at DESC);

-- ═══════════════════════ network ═══════════════════════

-- Padanan 9router: proxyPools. Bedanya: proxy_url di TEXT biasa (bukan blob)
-- supaya bisa di-mask saat dibaca, dan kolom credential_hint ngingetin
-- kredensial proxy ada di env, bukan di DB.
CREATE TABLE IF NOT EXISTS proxy_pools (
  id               TEXT PRIMARY KEY,
  name             TEXT NOT NULL UNIQUE,
  proxy_url        TEXT NOT NULL,            -- tanpa userinfo; userinfo via env
  no_proxy         TEXT,
  type             TEXT NOT NULL DEFAULT 'http',  -- http | socks5
  strict_proxy     INTEGER NOT NULL DEFAULT 0,    -- 1 = gagal kalau proxy mati, jangan fallback langsung
  credential_hint  TEXT,
  is_active        INTEGER NOT NULL DEFAULT 1,
  test_status      TEXT,
  last_tested_at   TEXT,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_proxy_active ON proxy_pools(is_active, test_status);

-- ═══════════════════════ pemakaian & trace ═══════════════════════

-- D3+D4: satu baris per request, sukses ATAU gagal. Tabel ini jadi sumber
-- kebenaran akuntansi; blob detail cuma buat debugging.
CREATE TABLE IF NOT EXISTS requests (
  id               TEXT PRIMARY KEY,
  ts               INTEGER NOT NULL,          -- epoch-ms saat mulai
  finished_at      INTEGER,
  api_key_id       TEXT REFERENCES api_keys(id) ON DELETE SET NULL,
  provider         TEXT,
  model            TEXT,
  connection_id    TEXT REFERENCES connections(id) ON DELETE SET NULL,
  node_id          TEXT,
  endpoint         TEXT NOT NULL,             -- /v1/chat/completions | /v1/models
  kind             TEXT NOT NULL DEFAULT 'chat',
  status           TEXT NOT NULL,             -- ok | client_error | upstream_error | rate_limited | aborted | locked | dry_run
  http_out         INTEGER,                   -- status yang dibalikin ke klien
  upstream_status  INTEGER,
  error_class      TEXT,                      -- quota_daily | pricing_blocked | throttle | sig_invalid | replay | timeout | stall | ...
  error_code       TEXT,                      -- kode vendor mentah (110/112/10605/103)
  ttft_ms          INTEGER,
  total_ms         INTEGER,
  frames           INTEGER,
  prompt           INTEGER NOT NULL DEFAULT 0,
  completion       INTEGER NOT NULL DEFAULT 0,
  reasoning        INTEGER NOT NULL DEFAULT 0,
  cached           INTEGER NOT NULL DEFAULT 0,
  cache_creation   INTEGER NOT NULL DEFAULT 0,
  total            INTEGER NOT NULL DEFAULT 0,
  usage_source     TEXT NOT NULL DEFAULT 'upstream',  -- upstream | estimated
  billable         INTEGER,
  credits          NUMERIC,                      -- satuan vendor, BUKAN usd (ADR-0001)
  credits_original NUMERIC,                      -- harga sebelum diskon vendor, kalau dikirim
  cost_usd         NUMERIC NOT NULL DEFAULT 0,
  tier             TEXT,
  budget_lane      TEXT,
  proxy_pool_id    TEXT,
  error_text       TEXT
);
CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts DESC);
CREATE INDEX IF NOT EXISTS idx_req_provider_model ON requests(provider, model);
CREATE INDEX IF NOT EXISTS idx_req_conn ON requests(connection_id);
CREATE INDEX IF NOT EXISTS idx_req_status ON requests(status);

-- Gantinya dump 7-file translator 9router (logs/translator/1_req_client.json ...
-- 7_res_client.json) jadi baris yang bisa diquery per request_id.
CREATE TABLE IF NOT EXISTS request_stages (
  id            INTEGER PRIMARY KEY,
  request_id    TEXT NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
  step          INTEGER NOT NULL,             -- 1..6, urutan pipeline
  name          TEXT NOT NULL,                -- client_in | openai_mid | provider_out | upstream_in | client_out
  direction     TEXT NOT NULL,                -- in | out
  payload_json  TEXT,
  bytes         INTEGER NOT NULL DEFAULT 0,
  truncated     INTEGER NOT NULL DEFAULT 0,   -- 1 = payload dipotong, lihat original_bytes
  original_bytes INTEGER,
  created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stages_req ON request_stages(request_id, step);

-- Rollup harian, ditulis dalam transaksi yang sama dengan baris requests
-- (pola usageDaily + _meta lifetime 9router, usageRepo.js:257-306).
CREATE TABLE IF NOT EXISTS usage_daily (
  date_key      TEXT NOT NULL,                -- YYYY-MM-DD (UTC)
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  requests      INTEGER NOT NULL DEFAULT 0,
  ok            INTEGER NOT NULL DEFAULT 0,
  errors        INTEGER NOT NULL DEFAULT 0,
  prompt        INTEGER NOT NULL DEFAULT 0,
  completion    INTEGER NOT NULL DEFAULT 0,
  cached        INTEGER NOT NULL DEFAULT 0,
  cost_usd      NUMERIC NOT NULL DEFAULT 0,
  updated_at    INTEGER NOT NULL,
  PRIMARY KEY(date_key, provider, model)
);

-- Snapshot kuota vendor. 9router gak nyimpen sama sekali (cache di RAM doang,
-- mati saat restart; quotaAutoPing.js:240-243 cuma nulis lastPingAt), jadi
-- budget gak survive restart. Tabel ini yang bikin survive.
CREATE TABLE IF NOT EXISTS quota_snapshots (
  id             INTEGER PRIMARY KEY,
  provider       TEXT NOT NULL,
  connection_id  TEXT,
  scope          TEXT NOT NULL,               -- nama kuota, mis. 'user', 'chat', 'plan'
  used           NUMERIC,
  total          NUMERIC,
  remaining      NUMERIC,
  remaining_pct  REAL,
  unlimited      INTEGER NOT NULL DEFAULT 0,
  reset_at       INTEGER,
  fetched_at     INTEGER NOT NULL,
  raw_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_quota_live ON quota_snapshots(provider, connection_id, fetched_at DESC);

-- Bucket anggaran harian (per provider|lane|api_key). Periode = YYYY-MM-DD UTC.
CREATE TABLE IF NOT EXISTS budgets (
  period_key     TEXT NOT NULL,
  scope          TEXT NOT NULL,               -- 'provider:qoder' | 'lane:interactive' | 'key:<id>' | 'global'
  token_limit    INTEGER,
  req_limit      INTEGER,
  used_tokens    INTEGER NOT NULL DEFAULT 0,
  used_requests  INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY(period_key, scope)
);

-- ═══════════════════════ konfigurasi runtime ═══════════════════════

-- Satu baris per kunci, nilai = JSON. Bukan satu blob raksasa: baris settings
-- 9router (tabel settings, id=1) udah kekotori 665 kunci numerik sampah hasil
-- spread `{...string}` di JS, dan password dashboard bcrypt ke-simpen di blob itu.
CREATE TABLE IF NOT EXISTS settings (
  key         TEXT PRIMARY KEY,
  value_json  TEXT NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
