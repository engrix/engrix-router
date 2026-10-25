"""Admin API: process-level actions -- restart the gateway from the dashboard.

POST /api/system/restart starts scripts/ops/router_restart.py DETACHED and
answers immediately. That helper owns the whole restart: it kills every
leftover server process, proves the port is actually free, starts a new
process, and only reports success once /health answers with a fresh uptime.

WHY THE HELPER AND NOT A SHELL CHAIN
------------------------------------
The previous implementation was fire-and-forget through four layers
(python -> wscript -> cmd /S /C -> powershell). Every layer could fail
silently and the only trace was an empty log, so "restart failed" had no
reason attached to it. Two concrete defects it hid:

  * router_service.ps1 `restart` discarded Stop-Router's result. If the port
    did not free within 20 s, Start-Router saw the port bound, printed
    "already running", returned TRUE and exited 0 -- a FALSE SUCCESS while the
    old process kept serving.
  * nothing reaped orphaned `python -m engrix_router.web.server` processes
    that were not holding the port (a crashed earlier start), so they kept the
    SQLite file busy and the next boot could stall.

GET /api/system/restart/status exposes the journal so the dashboard can show
the real stage and the real reason instead of guessing from uptime.

The marker file logs/restart-requested.json is still written: router_watchdog
reads it to avoid racing the helper during the down window.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from engrix_router.api import deps

router = APIRouter(prefix="/api/system", tags=["system"])

# Marker wajib tetap di <root repo>/logs/restart-requested.json: itu path yang
# dibaca router_watchdog.ps1. Root diturunkan dari file ini (src/engrix_router/
# api/) supaya tidak ada konfigurasi kedua yang memiliki lokasinya.
RESTART_GRACE_S = 90
ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "logs"
HELPER = ROOT / "scripts" / "ops" / "router_restart.py"
JOURNAL = LOG_DIR / "restart-journal.json"
MARKER = LOG_DIR / "restart-requested.json"

TERMINAL_STATES = ("done", "failed")


def _restart_log() -> Path:
    """Path of the helper log, resolved from LOG_DIR at call time.

    Deliberately not a frozen constant: when LOG_DIR moves (tests point it at a
    tmp dir), the log MUST move with it. A frozen constant made the test write
    into the production log that the live server holds open -- PermissionError,
    and worse: one test could pollute the operator's log.
    """
    return LOG_DIR / "router-restart.log"


def read_journal() -> dict[str, Any]:
    """Current restart journal, or a synthetic 'idle' record."""
    try:
        data = json.loads(JOURNAL.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"state": "idle", "stage": None, "reason": None}


def _spawn_helper(creation: int) -> int:
    """Start router_restart.py detached; return its pid.

    Python itself is the launcher now -- no wscript, no cmd, no VBScript.
    VBScript is being deprecated by Windows (VBScriptDeprecationAlert shows up
    in the Application log on this machine), so the fewer layers depend on it
    the better.
    """
    with open(_restart_log(), "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(HELPER), "--root", str(ROOT)],
            cwd=str(ROOT), stdout=log, stderr=log, creationflags=creation,
        )
    return proc.pid


@router.post("/restart", dependencies=[Depends(deps.require_admin)])
async def restart_gateway() -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    current = read_journal()
    if current.get("state") not in TERMINAL_STATES + ("idle",):
        return {
            "restarting": True,
            "already_running": True,
            "grace_s": RESTART_GRACE_S,
            "journal": current,
        }

    MARKER.write_text(
        json.dumps({"requested_at_ms": time.time_ns() // 1_000_000}), encoding="utf-8"
    )
    creation = 0
    if sys.platform == "win32":
        creation = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    helper_pid = _spawn_helper(creation)
    return {"restarting": True, "grace_s": RESTART_GRACE_S, "helper_pid": helper_pid}


@router.get("/restart/status", dependencies=[Depends(deps.require_admin)])
async def restart_status() -> dict[str, Any]:
    """Journal of the last restart -- polled by the dashboard while waiting.

    Survives the restart itself because it is a file, not process memory: the
    browser keeps polling and the NEW process serves the same journal.
    """
    return read_journal()
