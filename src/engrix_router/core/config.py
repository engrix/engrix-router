"""
Process configuration: paths and environment.

The `EROUTER_` prefix (Engrix + Router) is public API: it is deliberate and stable, and a
future rename would ship as a documented alias, never a silent break.

The boundary is deliberate so this never turns into config soup:
  - this file = anything that needs a restart: paths, host/port, env secrets,
    size limits, process-level HTTP/TLS deadlines.
  - the `settings` table (storage/settings.py) = anything an operator changes
    through the API/dashboard without restarting: routing strategy, sticky
    limit, cache TTLs, budget ceilings, retention.
A value lives in exactly one of the two, never both.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_DIR = Path(__file__).resolve().parents[1]      # src/engrix_router/
ROOT = PACKAGE_DIR.parents[1]                          # repo root (src-layout)

# .env di-root (gitignored) -- kredensial proxy + admin token + client base url.
load_dotenv(ROOT / ".env")

APP_NAME = "engrix-router"
VERSION = "0.1.0"


def _raw(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = _raw(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# ── lokasi data ──────────────────────────────────────────────────────────────
DATA_DIR = Path(_raw("EROUTER_DATA_DIR") or (ROOT / "data")).resolve()
DB_PATH = Path(_raw("EROUTER_DB_PATH") or (DATA_DIR / "engrix_router.sqlite3")).resolve()
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"          # ikut ke dalam wheel (package data)
LOG_DIR = Path(_raw("EROUTER_LOG_DIR") or (DATA_DIR / "logs")).resolve()
WEB_DIR = PACKAGE_DIR / "web"
CREDENTIALS_DIR = Path(_raw("EROUTER_CREDENTIALS_DIR") or (ROOT / "credentials")).resolve()
# Folder provider tambahan (ADR-0002). Isi biasanya checkout adapter privat, jadi
# adapter jalan tanpa install wheel. Separator = os.pathsep (';' di Windows).
PROVIDER_PATHS = [Path(p.strip()).expanduser().resolve()
                  for p in _raw("EROUTER_PROVIDERS_PATH").split(os.pathsep) if p.strip()]

# ── server ───────────────────────────────────────────────────────────────────
# Default-nya bind loopback. Gateway ini megang token upstream dan bisa ngabisin
# kuota, jadi jangan 0.0.0.0 kecuali owner minta eksplisit.
HOST = _raw("EROUTER_HOST", "127.0.0.1")
PORT = _int("EROUTER_PORT", 8450)
# engrix-agent pakai 8420 (produksi) / 8421 (sandbox). 8450 dipilih biar jauh
# dari tabrakan itu dan dari 20128 (9router di VPS).
CORS_ALLOW_ORIGINS = [o.strip() for o in _raw("EROUTER_CORS_ORIGINS").split(",") if o.strip()]

# ── autentikasi ──────────────────────────────────────────────────────────────
# Token admin (API /api/*). Sengaja TIDAK ada default: kalau env kosong,
# API admin mati (fail-closed) bukan malah buka. Padanan 9router nyimpen
# password bcrypt di blob settings dan kunci klien plaintext (apiKeysRepo.js:70-74)
# -- itu yang gak kita ulang.
ADMIN_TOKEN = _raw("EROUTER_ADMIN_TOKEN")
ADMIN_IS_CONFIGURED = bool(ADMIN_TOKEN)
# /v1* butuh api_key klien? 9router punya dua gate terpisah (middleware +
# settings.requireApiKey di handler); kita satu gate, jelas.
REQUIRE_CLIENT_KEY = _bool("EROUTER_REQUIRE_CLIENT_KEY", True)

# ── batas tubuh request ──────────────────────────────────────────────────────
MAX_REQUEST_BYTES = _int("EROUTER_MAX_REQUEST_BYTES", 6 * 1024 * 1024)
# Padanan observabilityMaxJsonSize 9router (default 5 KiB) -- di kita dalam byte.
MAX_STAGE_BYTES = _int("EROUTER_MAX_STAGE_BYTES", 64 * 1024)

# ── deadline upstream ────────────────────────────────────────────────────────
# Kontrak dengan engrix-agent: deadline connect-race dia 30s
# (engrix-agent/agent/streaming.py:62 _STREAM_CONNECT_TIMEOUT = 30.0).
# Gateway wajib mutusin sebelum itu supaya dia gak bakar round ke-2/3 dobel.
UPSTREAM_CONNECT_TIMEOUT_S = _float("EROUTER_UPSTREAM_CONNECT_TIMEOUT_S", 20.0)
UPSTREAM_FIRST_CHUNK_TIMEOUT_S = _float("EROUTER_UPSTREAM_FIRST_CHUNK_TIMEOUT_S", 25.0)
UPSTREAM_STALL_TIMEOUT_S = _float("EROUTER_UPSTREAM_STALL_TIMEOUT_S", 120.0)
UPSTREAM_TOTAL_TIMEOUT_S = _float("EROUTER_UPSTREAM_TOTAL_TIMEOUT_S", 300.0)

# ── logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL = _raw("EROUTER_LOG_LEVEL", "info").lower()
LOG_RING_SIZE = _int("EROUTER_LOG_RING_SIZE", 2000)
LOG_FILE_ENABLED = _bool("EROUTER_LOG_FILE", True)
LOG_FILE_MAX_BYTES = _int("EROUTER_LOG_FILE_MAX_BYTES", 5 * 1024 * 1024)
LOG_FILE_BACKUPS = _int("EROUTER_LOG_FILE_BACKUPS", 3)
# Header yang WAJIB di-redact di semua jalur log. Di 9router fungsi
# maskSensitiveHeaders beneran mengembalikan header apa adanya
# (open-sse/utils/requestLogger.js:72-91, kodenya dikomentari).
REDACT_HEADERS = [
    h.strip().lower()
    for h in _raw(
        "EROUTER_REDACT_HEADERS",
        "authorization,x-api-key,x-goog-api-key,cookie,set-cookie,x-gw-user-id",
    ).split(",")
    if h.strip()
]

# ── mode ─────────────────────────────────────────────────────────────────────
# dry_run: engine nulis trace lengkap tapi gak nembak upstream. Dipakai buat
# nge-diff payload kita vs 9router tanpa bakar kuota.
DRY_RUN = _bool("EROUTER_DRY_RUN", False)
TEST_MODE = _bool("EROUTER_TEST_MODE", False)  # dipakai tests/


def ensure_runtime_dirs() -> None:
    """
    Called once at startup; every process writes only here.
    """
    for path in (DATA_DIR, LOG_DIR, CREDENTIALS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def summary() -> dict[str, object]:
    """
    For /health and the startup line. Contains no secrets.
    """
    return {
        "app": APP_NAME,
        "version": VERSION,
        "host": HOST,
        "port": PORT,
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "admin_configured": ADMIN_IS_CONFIGURED,
        "require_client_key": REQUIRE_CLIENT_KEY,
        "dry_run": DRY_RUN,
        "log_level": LOG_LEVEL,
        "deadlines_s": {
            "connect": UPSTREAM_CONNECT_TIMEOUT_S,
            "first_chunk": UPSTREAM_FIRST_CHUNK_TIMEOUT_S,
            "stall": UPSTREAM_STALL_TIMEOUT_S,
            "total": UPSTREAM_TOTAL_TIMEOUT_S,
        },
    }
