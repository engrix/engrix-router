"""TASK-60 regressions: dashboard restart must actually execute the helper.

Root cause (measured, sandbox isolation matrix): a DETACHED_PROCESS spawn of
powershell.exe (console app) dies instantly on this machine before running
anything -- 4 owner clicks produced 4x "POST /api/system/restart 200 OK" and
zero helper executions (no stop, no start, pid untouched).
Fix: the endpoint generates a .vbs launcher and spawns it via wscript.exe
(GUI subsystem, no console needed -- same proven pattern as the watchdog).
These tests pin: marker written, launcher generated with a restart command,
and wscript.exe (NOT powershell.exe) as the spawned executable.
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engrix_router.api import admin_system, deps


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(admin_system, "LOG_DIR", tmp_path)
    app = FastAPI()
    app.dependency_overrides[deps.require_admin] = lambda: None
    app.include_router(admin_system.router)
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return None

    monkeypatch.setattr(admin_system.subprocess, "Popen", fake_popen)
    with TestClient(app) as c:
        c.captured = captured  # type: ignore[attr-defined]
        yield c


def test_restart_writes_marker_and_spawns_wscript(client, tmp_path):
    resp = client.post("/api/system/restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["restarting"] is True

    # Marker: fresh, parseable, millisecond timestamp.
    marker = tmp_path / "restart-requested.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    age_ms = time.time_ns() // 1_000_000 - data["requested_at_ms"]
    assert 0 <= age_ms < 5_000

    # Spawn target: wscript.exe (GUI subsystem), never powershell.exe directly.
    argv = client.captured["argv"]  # type: ignore[attr-defined]
    assert argv[0] == "wscript.exe"
    assert not any("powershell" in str(a).lower() for a in argv)

    # Launcher: generated next to the marker, runs the service restart,
    # appends to the restart log so failures are not silent.
    vbs = tmp_path / "router-restart-launcher.vbs"
    assert vbs.exists()
    text = vbs.read_text(encoding="ascii")
    assert "router_service.ps1" in text
    assert " restart" in text
    assert "router-restart.log" in text
    assert 'sh.Run "' in text
