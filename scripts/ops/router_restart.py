"""Restart helper for engrix_router.web.server -- one observable layer.

WHY THIS FILE EXISTS
--------------------
The previous restart path was fire-and-forget through FOUR layers:

    python (API) -> wscript.exe -> cmd.exe /S /C -> powershell.exe -File

Every layer could fail silently. `logs/router-restart.log` stayed empty and
there was no way to tell "the helper never started" apart from "the helper
started and the stop/start failed". On top of that, `router_service.ps1
restart` discarded the result of Stop-Router: if the port did not free in
20 s, Start-Router saw the port still bound, printed "already running",
returned TRUE and exited 0 -- a FALSE SUCCESS while the old process kept
running. Windows is also retiring VBScript (VBScriptDeprecationAlert appears
in the Application log), so depending on wscript is a moving target.

This helper replaces all of that with ONE process that records every stage to
a journal the dashboard can poll. Failure is explicit and carries a reason;
success is only declared after /health answers with a NEW uptime.

Single implementation, three callers: the dashboard API, router_service.ps1
(CLI + watchdog), and any operator shell.

CLI:
    python scripts/ops/router_restart.py [--port 8450] [--root <dir>]
                                        [--journal <path>] [--no-wait]
Exit codes: 0 done, 1 failed (reason in the journal), 2 bad arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SERVER_MODULE = "engrix_router.web.server"
DEFAULT_PORT = 8450
# Root diturunkan dari lokasi file ini (scripts/ops/ -> root repo), bukan
# ditulis absolut: path pemilik tidak boleh nongkrong di kode yang dibagikan.
DEFAULT_ROOT = Path(__file__).resolve().parents[2]

STOP_TIMEOUT_S = 25.0
HEALTH_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 0.4

# Windows: program konsol yang di-spawn proses TANPA konsol sendiri (pythonw.exe,
# atau python.exe yang dijalankan detached) dikasih konsol BARU. Kalau Windows
# Terminal jadi terminal default, konsol baru itu jadi jendela yang KELIHATAN di
# desktop operator. Terukur di mesin operator: tiap panggilan `netstat` dan
# `tasklist` melahirkan WindowsTerminal sendiri dengan HWND bukan nol.
# CREATE_NO_WINDOW bikin child-nya dapat konsol tersembunyi, jadi tidak ada yang
# pernah tergambar.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def run_hidden(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a console program without ever creating a visible console window.

    EVERY subprocess in this module MUST go through here -- a single bare
    `subprocess.run` is enough to flash a terminal window at the operator.
    """
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        creationflags=_NO_WINDOW,
    )


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
def write_journal(path: Path, **fields: Any) -> None:
    """Merge fields into the journal file; never raises.

    A broken journal must not abort a restart that is already in progress, so
    every failure here is swallowed on purpose. The write is atomic (temp file
    + replace) because the dashboard polls this file WHILE the helper writes
    it -- a half-written JSON would make the UI fall back to guessing.
    """
    payload: dict[str, Any] = {}
    try:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    payload.update(fields)
    payload["updated_at_ms"] = int(time.time() * 1000)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# process discovery (netstat -- no CIM, it is slow in this environment)
# --------------------------------------------------------------------------- #
def listener_pid(port: int) -> int | None:
    """Return the PID holding a LISTENING socket on `port`, or None."""
    try:
        out = run_hidden(["netstat", "-ano", "-p", "TCP"], timeout=20).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[3].upper() != "LISTENING":
            continue
        local, pid_text = parts[1], parts[4]
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid > 0:
            return pid
    return None


def orphan_pids(root: Path, keep: int | None) -> list[int]:
    """Return PIDs running SERVER_MODULE that are not the current listener.

    These are the leftovers of a previously failed start: they hold the SQLite
    file (and sometimes the port) and make the next restart look broken.
    """
    if os.name != "nt":
        return []
    script = (
        "import json,sys\n"
        "try:\n"
        "    import psutil\n"
        "except Exception:\n"
        "    print('[]'); sys.exit(0)\n"
        "out=[]\n"
        f"needle={SERVER_MODULE!r}\n"
        "for p in psutil.process_iter(['pid','name','cmdline']):\n"
        "    try:\n"
        "        cl=p.info.get('cmdline') or []\n"
        "        if any(needle in str(a) for a in cl) and 'router_restart' not in ' '.join(cl):\n"
        "            out.append(p.info['pid'])\n"
        "    except Exception:\n"
        "        pass\n"
        "print(json.dumps(out))\n"
    )
    try:
        raw = run_hidden([sys.executable, "-c", script], timeout=30).stdout.strip()
        pids = json.loads(raw or "[]")
    except Exception:
        return []
    return [int(p) for p in pids if isinstance(p, int) and int(p) != keep]


def kill_pid(pid: int, timeout: float = 15.0) -> bool:
    """Terminate a PID and wait until it is really gone."""
    if os.name == "nt":
        run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"])
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    return not pid_alive(pid)


def pid_alive(pid: int) -> bool:
    """True when the PID still exists."""
    if os.name == "nt":
        out = run_hidden(["tasklist", "/FI", f"PID eq {pid}", "/NH"]).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# health probe
# --------------------------------------------------------------------------- #
def health_uptime(port: int, timeout: float = 2.0) -> float | None:
    """Return uptime_s from /health, or None when the server is not answering."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return float(data.get("uptime_s") or 0.0)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None


def port_free(port: int) -> bool:
    """True when nothing is listening on `port`."""
    return listener_pid(port) is None


def wait_port_free(port: int, timeout: float) -> bool:
    """Poll until the port is free or `timeout` elapses; return the final state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_free(port):
            return True
        time.sleep(POLL_INTERVAL_S)
    return port_free(port)


# --------------------------------------------------------------------------- #
# main flow
# --------------------------------------------------------------------------- #
def restart(root: Path, port: int, journal: Path, wait: bool) -> int:
    """Stop every server process, prove the port is free, start, verify health.

    Returns 0 only when the new process answers /health (unless wait=False).
    """
    stdout_log = root / "logs" / "router-stdout.log"
    stderr_log = root / "logs" / "router-stderr.log"
    (root / "logs").mkdir(parents=True, exist_ok=True)

    write_journal(journal, state="stopping", reason=None, stage="stop",
                  port=port, root=str(root), old_pid=listener_pid(port))

    # 1. matikan SEMUA proses server sisa, bukan cuma pemegang port
    old_pid = listener_pid(port)
    leftovers = orphan_pids(root, keep=None)
    killed: list[int] = []
    for pid in leftovers:
        if kill_pid(pid):
            killed.append(pid)
    if old_pid and old_pid not in killed and pid_alive(old_pid):
        if kill_pid(old_pid):
            killed.append(old_pid)

    # 2. Port WAJIB lepas sebelum apa pun dinyalakan -- inilah pemeriksaan yang
    #    dilewati skrip lama, dan sebab restart bisa "sukses" padahal proses
    #    sebelumnya masih jalan.
    if not wait_port_free(port, STOP_TIMEOUT_S):
        holder = listener_pid(port)
        write_journal(
            journal, state="failed", stage="stop",
            reason=f"port {port} is still held by pid {holder} after {STOP_TIMEOUT_S:.0f}s",
            killed=killed,
        )
        return 1

    write_journal(journal, state="starting", stage="start", killed=killed)

    # 3. nyalakan server, terlepas dari helper ini
    creation = 0
    if os.name == "nt":
        creation = (subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW)
    with open(stdout_log, "ab") as out, open(stderr_log, "ab") as err:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", SERVER_MODULE],
            cwd=str(root), stdout=out, stderr=err, creationflags=creation,
        )
    write_journal(journal, new_pid=proc.pid, stage="waiting_health")

    if not wait:
        write_journal(journal, state="started", stage="done", reason=None)
        return 0

    # 4. sukses HANYA diakui setelah /health menjawab dengan uptime baru
    deadline = time.time() + HEALTH_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            tail = _tail(stderr_log)
            write_journal(
                journal, state="failed", stage="start",
                reason=f"server exited with code {proc.returncode} before becoming healthy",
                stderr_tail=tail,
            )
            return 1
        uptime = health_uptime(port)
        if uptime is not None:
            write_journal(
                journal, state="done", stage="done", reason=None,
                uptime_s=uptime, new_pid=proc.pid,
            )
            return 0
        time.sleep(POLL_INTERVAL_S)

    tail = _tail(stderr_log)
    write_journal(
        journal, state="failed", stage="waiting_health",
        reason=f"/health did not answer within {HEALTH_TIMEOUT_S:.0f}s",
        stderr_tail=tail,
    )
    return 1


def _tail(path: Path, limit: int = 1500) -> str:
    """Return the last `limit` characters of a log file, or '' when unreadable."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return text[-limit:]


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; the exit code is the operator-facing result."""
    ap = argparse.ArgumentParser(description="Restart the engrix router with a journal.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    ap.add_argument("--journal", default="")
    ap.add_argument("--no-wait", action="store_true",
                    help="start and return without waiting for /health")
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not (root / "src" / "engrix_router").is_dir():
        print(f"not a router root: {root}", file=sys.stderr)
        return 2
    journal = Path(args.journal) if args.journal else root / "logs" / "restart-journal.json"
    try:
        return restart(root, args.port, journal, wait=not args.no_wait)
    except Exception as exc:  # noqa: BLE001 - alasannya harus sampai ke journal
        write_journal(journal, state="failed", stage="crashed",
                      reason=f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
