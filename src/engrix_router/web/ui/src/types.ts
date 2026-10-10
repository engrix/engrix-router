// Bentuk respons API gateway -- diturunkan dari payload live /api/* (TASK-43),
// bukan dari tebakan. Semua field yang dibaca UI ada di sini supaya TS memaksa
// kami sadar kalau shape backend berubah.

export interface ProviderDef {
  id: string;
  display_name: string;
  category: string;
  prefixes: string[];
  base_url: string;
  auth_modes: string[];
  probe_tier: string;
  features: string[];
  oauth: boolean;
  has_usage: boolean;
  models_declared: string[];
  connections: number;
  active_connections: number;
  is_node: boolean;
}

export interface Health {
  connection_id: string;
  test_status: string;
  error_code: string | null;
  error_class: string | null;
  last_error: string | null;
  rate_limited_until: number | null;
  last_used_at: number | null;
  provider?: string;
  name?: string | null;
  locks?: { model: string; locked_until: number }[];
}

export interface Connection {
  id: string;
  provider: string;
  node_id: string | null;
  auth_type: string;
  name: string | null;
  email: string | null;
  priority: number;
  is_active: boolean;
  proxy_pool_id: string | null;
  created_at: string;
  updated_at: string;
  has_api_key?: boolean;
  has_access_token?: boolean;
  // epoch-ms atau ISO string -- kedua bentuk ada di backend, dcount() yang menormalkan
  expires_at?: number | string | null;
  health?: Health;
  provider_specific?: Record<string, unknown>;
}

export interface QuotaReading {
  scope: string;
  used: number | null;
  total: number | null;
  remaining: number | null;
  remaining_pct: number | null;
  unlimited: boolean;
  reset_at: number | null;
}

export interface QuotaProvider {
  connection_id: string;
  name: string;
  provider: string;
  available: boolean;
  cached: boolean;
  fetched_at: number;
  latency_ms: number | null;
  // only present when the backend says available=false (no usage spec /
  // vendor error) -- the UI shows it instead of a blank "no quota" claim
  reason?: string;
  readings: QuotaReading[];
}

export interface QuotaWorst {
  provider: string;
  connection_id: string;
  scope: string;
  remaining: number;
  remaining_pct: number;
  reset_at: number | null;
  fetched_at: number;
}

export interface QuotaSnapshot {
  providers: QuotaProvider[];
  worst?: QuotaWorst | null;
  error?: string;
}

export interface DayChart {
  label: string;
  requests: number;
  tokens: number;
  cost: number;
}

export interface StatsTotals {
  requests: number;
  ok: number;
  errors: number;
  tokens: number;
  prompt: number;
  cached: number;
  cost: number;
}

export interface ProviderUsage {
  provider: string;
  requests: number;
  tokens: number;
  cost: number;
  errors?: number;
  model?: string;
}

export interface BudgetRow {
  scope: string;
  used_tokens: number;
  token_limit: number | null;
  used_pct: number | null;
}

export interface Stats {
  totals: StatsTotals;
  by_provider: ProviderUsage[];
  by_model: ProviderUsage[];
  budget: { rows: BudgetRow[] };
}

export interface State {
  process: {
    app: string; version: string; host: string; port: number;
    admin_configured: boolean; require_client_key: boolean; dry_run: boolean;
    log_level: string;
  };
  providers: { id: string; prefixes: string[]; base_url: string; is_node: boolean }[];
  health: Health[];
  drift: { tripped: boolean; until_ms: number; until_iso: string | null; reason: string };
}

export interface Healthz {
  ok: boolean;
  version: string;
  uptime_s: number;
  upstream_frozen: boolean;
  providers: number;
  providers_failed: Record<string, string>;
  settings_keys: number;
}

export interface RequestRow {
  id: string;
  ts: number;
  provider: string;
  model: string;
  connection_id: string;
  status: string;
  http_out: number;
  error_class: string | null;
  ttft_ms: number | null;
  total_ms: number | null;
  total: number | null;
  cost_usd: number;
}

export interface TraceStage {
  step: string;
  name: string;
  direction: string;
  bytes: number;
  payload_json?: string | null;
  truncated?: boolean;
  original_bytes?: number;
}

export interface TraceItem extends RequestRow {
  finished_at: number | null;
  api_key_id: string | null;
  endpoint: string;
  kind: string;
  upstream_status: number | null;
  error_code: string | null;
  error_text: string | null;
  frames: number | null;
  prompt: number | null;
  completion: number | null;
  reasoning: number | null;
  cached: number | null;
  usage_source: string | null;
  billable: number | null;
  tier: string | null;
  proxy_pool_id: string | null;
  stages: TraceStage[];
}

export interface NodeRow {
  id: string;
  name: string;
  prefix: string;
  base_url: string;
  type: string;
  api_type: string;
  is_active: boolean;
}

export interface KeyRow {
  id: string;
  name: string;
  key_prefix: string;
  machine_id: string | null;
  is_active: boolean;
  created_at: string;
  last_used_at: string | null;
}

export interface PoolRow {
  id: string;
  name: string;
  proxy_url: string;
  type: string;
  strict_proxy: boolean;
  test_status: string | null;
  last_error: string | null;
  bound_connection_count?: number;
  usage?: { requests?: number } | null;
}

export interface SettingsDoc {
  values: Record<string, unknown>;
  schema: Record<string, { default: unknown; type: string; doc: string }>;
}

export interface LogLine {
  ts: number;
  level: string;
  ns: string;
  rid: string | null;
  line: string;
}

export interface CatalogRow {
  ok: boolean;
  models?: { id: string; name: string; context_length: number | null; supports_vision: boolean; supports_tools: boolean }[];
  error?: string;
}

export interface ModelTestRow {
  model: string;
  ok: boolean;
  latency_ms: number | null;
  error_class?: string | null;
}
