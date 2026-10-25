"""Watchdog for engrix_router.web.server -- revive the gateway when it is down.

WHY THIS FILE EXISTS
--------------------
The PowerShell watchdog was reachable only through a wscript.exe launcher
(`router_watchdog_hidden.vbs`) because a scheduled task running powershell.exe
directly flashes a console window on every trigger. That made the revival path
depend on FOUR layers -- Task Scheduler -> wscript -> powershell -> python --
and every layer could fail without leaving a single line in the log.

Measured on the machine this was written for: a restart requested from the
dashboard at 03:00:41 left a fresh `logs/restart-requested.json` and a fresh
`.vbs` launcher, but `logs/router-restart.log` was untouched since 21:25:38 and
`logs/watchdog.log` since 18:54:08. Both files that were supposed to record the
outcome stayed empty, so the operator saw "restart failed" with no reason. A
watchdog that cannot be observed is not a watchdog.

This module removes wscript, cmd and powershell from the revival path. It is
started by pythonw.exe (a GUI-subsystem executable, so no console flashes) and
it writes its own log. It also replaces the `restart-requested.json` marker
with an explicit state machine, because the old marker could not distinguish
"a restart is in flight" from "a restart was requested and died" -- which is
exactly the case that left the gateway down for 150 s.

MODES
-----
    python router_watchdog.py --once     # one check, used by Task Scheduler
    python router_watchdog.py --loop     # continuous, mutex-guarded

The helper `router_restart.py` owns stop+start. This module only decides WHEN
to call it, so there is still exactly one implementation of the restart.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_MODULE = "engrix_router.web.server"
HELPER = ROOT / "scripts" / "ops" / "router_restart.py"
DEFAULT_PORT = 8450

# Marker restart yang umurnya di bawah ini berarti "restart memang sedang jalan".
# Dipilih jauh di atas anggaran helper sendiri (stop 25 s + health 60 s) supaya
# watchdog tidak pernah balapan dengannya, dan jauh di bawah deadline 150 s
# dashboard supaya helper yang mati tetap dihidupkan dalam jendela yang dipantau UI.
RESTART_FRESH_S = 120
# Setelah revival gagal, tunggu selama ini sebelum coba lagi. Tanpa jeda ini tiap
# tick scheduler akan spawn helper baru sementara yang sebelumnya masih
# menghentikan proses lama -- dua helper saling membunuh hasil kerja.
RETRY_COOLDOWN_S = 30

# Windows: program konsol yang dijalankan proses TANPA konsol sendiri (pythonw.exe)
# dikasih konsol BARU -- dan kalau Windows Terminal jadi terminal default, konsol
# baru itu jadi jendela yang benar-benar KELIHATAN.
#
# Terukur di mesin operator: tiap tick scheduled task menjalankan `netstat` lalu
# `tasklist`, dan masing-masing melahirkan WindowsTerminal sendiri dengan HWND
# bukan nol (tertangkap: 03:36:09.070 hwnd=1705238, 03:36:09.877 hwnd=2884978).
# Dua jendela per menit, terus-menerus, di desktop operator. CREATE_NO_WINDOW
# bikin child-nya dapat konsol tersembunyi, jadi tidak ada yang pernah tergambar.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_hidden(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run a console program without ever creating a visible console window.

    EVERY subprocess in this module MUST go through here. A single bare
    `subprocess.run` is enough to flash a window on the operator's desktop,
    which is exactly the defect this helper exists to prevent.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
    )


def log_path() -> Path:
    """Watchdog log, resolved at call time so tests can redirect LOG_DIR."""
    return Path(os.environ.get("EROUTER_LOG_DIR") or (ROOT / "logs")) / "watchdog.log"


def write_log(message: str) -> None:
    """Append one timestamped line; never raise (a log failure must not stop a revival)."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
        if path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.old"))
    except OSError:
        pass


def listener_pid(port: int) -> int | None:
    """PID holding a LISTENING socket on `port`, or None.

    netstat instead of psutil: it is present on every Windows install and is
    the same source the service script used, so behaviour does not change when
    the dependency graph does.
    """
    try:
        out = run_hidden(["netstat", "-ano", "-p", "TCP"], timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def is_alive(port: int) -> bool:
    """True when a python process is listening on the port."""
    pid = listener_pid(port)
    if pid is None:
        return False
    try:
        out = run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/NH"], timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "python" in out.lower()


def read_journal(port: int) -> dict:
    """Current restart journal, or {} when absent/unreadable."""
    try:
        raw = (ROOT / "logs" / "restart-journal.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def restart_in_flight(port: int) -> tuple[bool, str]:
    """Decide whether a restart is genuinely still running.

    The old implementation only looked at the age of `restart-requested.json`,
    so a helper that died mid-restart still suppressed the watchdog for the
    full grace window -- the gateway stayed down and nobody was told.

    Here a restart counts as in flight only when the marker is fresh AND the
    journal does not say the helper already finished. Anything else (stale
    marker, journal failed, journal done) means the watchdog owns the port.
    """
    marker = ROOT / "logs" / "restart-requested.json"
    try:
        age_s = time.time() - marker.stat().st_mtime
    except OSError:
        return False, "no marker"
    if age_s > RESTART_FRESH_S:
        return False, f"marker stale ({age_s:.0f}s)"

    journal = read_journal(port)
    state = str(journal.get("state") or "")
    if state in ("done", "failed"):
        reason = journal.get("reason") or "no reason recorded"
        return False, f"helper finished state={state} reason={reason}"
    return True, f"helper running state={state or 'unknown'} age={age_s:.0f}s"


def revive(port: int) -> bool:
    """Run the restart helper once; return True when the port came back."""
    write_log(f"ROUTER DOWN (no listener on port {port}) -> running router_restart.py")
    try:
        proc = run_hidden(
            [sys.executable, "-u", str(HELPER), "--root", str(ROOT), "--port", str(port)],
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        write_log(f"REVIVE FAILED -- helper could not start: {type(exc).__name__}: {exc}")
        return False
    for line in (proc.stdout or "").splitlines():
        write_log("  " + line)
    for line in (proc.stderr or "").splitlines():
        write_log("  stderr: " + line)
    if is_alive(port):
        write_log(f"REVIVED OK (pid={listener_pid(port)}, helper exit={proc.returncode})")
        return True
    journal = read_journal(port)
    write_log(
        "REVIVE FAILED -- will retry next cycle"
        f" (helper exit={proc.returncode}, journal state={journal.get('state')},"
        f" reason={journal.get('reason')})"
    )
    return False


def check_once(port: int) -> bool:
    """One watchdog pass; returns True when it did something."""
    if is_alive(port):
        return False
    pending, why = restart_in_flight(port)
    if pending:
        write_log(f"restart in flight -- watchdog skips this cycle ({why})")
        return False
    write_log(f"restart not in flight ({why})")
    revive(port)
    return True


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: --once (default) or --loop."""
    ap = argparse.ArgumentParser(description="Revive engrix_router.web.server when down.")
    ap.add_argument("--once", action="store_true", help="single check (default)")
    ap.add_argument("--loop", action="store_true", help="continuous loop")
    ap.add_argument("--interval", type=int, default=60, help="loop interval in seconds")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)

    if not args.loop:
        check_once(args.port)
        return 0

    # Loop kedua akan berkelahi dengan yang pertama: dua-duanya mencoba
    # me-restart proses yang sama. Mutex-nya dinamai lokal supaya mati
    # bersama sesinya.
    if os.name == "nt":
        import ctypes  # noqa: PLC0415 - only needed on Windows
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Local\\EngrixRouterWatchdogLoop")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            write_log("another watchdog loop already running -> exit")
            return 0
    write_log(f"watchdog loop started (check every {args.interval}s)")
    while True:
        try:
            if check_once(args.port):
                time.sleep(RETRY_COOLDOWN_S)
            else:
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:  # noqa: BLE001 - the loop must survive anything
            write_log(f"watchdog cycle crashed: {type(exc).__name__}: {exc}")
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
