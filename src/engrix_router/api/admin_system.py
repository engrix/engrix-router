"""Admin API: process-level actions -- restart the gateway from the dashboard.

POST /api/system/restart answers immediately after arming two things:
  1. logs/restart-requested.json -- a marker the watchdog reads so it does not
     race the restart helper (it skips its own revival for a grace window);
  2. a DETACHED powershell running router_service.ps1 restart, spawned with
     DETACHED_PROCESS so it survives this process's death -- a plain child
     would be killed together with the very server it is restarting. The
     response still reaches the browser before the old server goes down.
If the helper fails and the port stays dead past the grace window, the
watchdog revives the server anyway: restart must never become a way to stay
down.

WHY wscript (TASK-60): powershell.exe is a console app; on this machine a
DETACHED powershell dies instantly before executing anything (measured
isolation matrix, 3/3 variants dead with DETACHED_PROCESS, alive without).
wscript.exe is a GUI-subsystem executable -- no console needed -- and the
same launcher pattern already runs the scheduled-task watchdog every minute.
The .vbs launcher is generated here (not committed) so the router root is
never duplicated; it runs router_service.ps1 restart with output appended to
logs/router-restart.log for post-mortem.
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

# Marker harus persis di path yang dibaca router_watchdog.ps1:
# <repo root>/logs/restart-requested.json. Root diturunkan dari file ini
# (src/engrix_router/api/) supaya gak ada config kedua yang megang lokasinya.
RESTART_GRACE_S = 90
ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = ROOT / "logs"
SERVICE = ROOT / "scripts" / "ops" / "router_service.ps1"
VBS_LAUNCHER = "router-restart-launcher.vbs"
RESTART_LOG = "router-restart.log"


def _spawn_restart_helper(creation: int) -> None:
    """Generate the launcher .vbs and run it via wscript, detached.

    Python builds the VBS line and doubles every quote itself (VBS escapes
    a literal quote by doubling), so the cmd /S /C quoting stays correct.
    """
    run_arg = (
        'cmd.exe /S /C "powershell.exe -NoProfile -ExecutionPolicy Bypass '
        f'-WindowStyle Hidden -File "{SERVICE}" restart >> "{LOG_DIR / RESTART_LOG}" 2>&1"'
    )
    vbs_text = (
        'Set sh = CreateObject("WScript.Shell")\n'
        f'sh.Run "{run_arg.replace(chr(34), chr(34) * 2)}", 0, False\n'
    )
    (LOG_DIR / VBS_LAUNCHER).write_text(vbs_text, encoding="ascii")
    subprocess.Popen(
        ["wscript.exe", str(LOG_DIR / VBS_LAUNCHER)],
        cwd=str(ROOT), creationflags=creation,
    )


@router.post("/restart", dependencies=[Depends(deps.require_admin)])
async def restart_gateway() -> dict[str, Any]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    (LOG_DIR / "restart-requested.json").write_text(
        json.dumps({"requested_at_ms": time.time_ns() // 1_000_000}), encoding="utf-8"
    )
    creation = 0
    if sys.platform == "win32":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    _spawn_restart_helper(creation)
    return {"restarting": True, "grace_s": RESTART_GRACE_S}
