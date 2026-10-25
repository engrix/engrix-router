"""TASK-60 regressions: dashboard restart must actually execute the helper.

RIWAYAT (kenapa berkas ini berubah)
-----------------------------------
Akar masalah yang dulu terukur: spawn DETACHED_PROCESS atas powershell.exe
(aplikasi console) mati seketika di mesin ini sebelum menjalankan apa pun --
4 klik owner menghasilkan 4x "POST /api/system/restart 200 OK" dan nol
eksekusi helper (tidak ada stop, tidak ada start, pid tidak berubah).
Perbaikan waktu itu: endpoint membuat launcher .vbs lalu men-spawn-nya lewat
wscript.exe (subsystem GUI, tidak butuh console).

Sekarang rantai itu DIGANTI, bukan dilonggarkan. Alasannya dua, keduanya
terukur dan bukan selera:

  * wscript -> cmd /S /C -> powershell adalah empat lapisan yang bisa gagal
    tanpa suara. Bukti lapangan: logs/restart-requested.json bertanggal
    22:11:06 sementara logs/router-restart.log terakhir menyentuh 21:25:38 --
    kliknya terjadi, helper-nya tidak pernah jalan, dan tidak ada satu pun
    catatan yang bisa dibaca untuk tahu kenapa.
  * Windows sedang memensiunkan VBScript: VBScriptDeprecationAlert muncul
    berulang di Application log mesin ini. Bergantung pada wscript adalah
    sasaran yang bergerak.

Helper python yang dipanggil langsung menghapus wscript, cmd, DAN powershell
dari jalur restart, sekaligus mencatat tiap tahap ke restart-journal.json.

Yang dikunci berkas ini karena itu bukan lagi "argv[0] == wscript.exe", tapi
sifat yang membuat restart bisa dipercaya:
  * marker tetap ditulis (watchdog membacanya untuk grace window),
  * yang di-spawn adalah helper restart, bukan shell apa pun,
  * helper dijalankan DETACHED supaya selamat dari kematian server,
  * endpoint status membaca journal yang sama.
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
    monkeypatch.setattr(admin_system, "JOURNAL", tmp_path / "restart-journal.json")
    monkeypatch.setattr(admin_system, "MARKER", tmp_path / "restart-requested.json")
    app = FastAPI()
    app.dependency_overrides[deps.require_admin] = lambda: None
    app.include_router(admin_system.router)
    captured: dict = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return type("P", (), {"pid": 12345})()

    monkeypatch.setattr(admin_system.subprocess, "Popen", fake_popen)
    with TestClient(app) as c:
        c.captured = captured  # type: ignore[attr-defined]
        yield c


def test_restart_writes_marker_and_spawns_the_helper(client, tmp_path):
    resp = client.post("/api/system/restart")
    assert resp.status_code == 200
    body = resp.json()
    assert body["restarting"] is True

    # Marker: segar, bisa diparse, timestamp milidetik. Watchdog membacanya
    # untuk tahu harus menahan diri selama jendela restart.
    marker = tmp_path / "restart-requested.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    age_ms = time.time_ns() // 1_000_000 - data["requested_at_ms"]
    assert 0 <= age_ms < 5_000

    # Yang di-spawn adalah helper restart python -- bukan shell.
    argv = client.captured["argv"]  # type: ignore[attr-defined]
    joined = " ".join(str(a).lower() for a in argv)
    assert "router_restart.py" in joined, f"helper not spawned: {argv}"
    assert not any(tok in joined for tok in ("wscript", "cscript", "powershell", "cmd.exe")), (
        f"restart must not go through a shell chain: {argv}"
    )

    # Detached: tanpa itu helper mati bersama server yang sedang di-restart-nya.
    flags = client.captured["kwargs"].get("creationflags", 0)  # type: ignore[attr-defined]
    assert flags & admin_system.subprocess.DETACHED_PROCESS
    assert flags & admin_system.subprocess.CREATE_NEW_PROCESS_GROUP


def test_restart_status_reports_the_journal(client, tmp_path):
    journal = tmp_path / "restart-journal.json"
    journal.write_text(
        json.dumps({"state": "failed", "stage": "stop", "reason": "port 8450 is still held by pid 7"}),
        encoding="utf-8",
    )
    resp = client.get("/api/system/restart/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "failed"
    assert "still held by pid 7" in body["reason"]


def test_restart_status_is_idle_without_a_journal(client, tmp_path):
    assert not (tmp_path / "restart-journal.json").exists()
    body = client.get("/api/system/restart/status").json()
    assert body["state"] == "idle"
    assert body["reason"] is None


def test_second_click_does_not_spawn_a_competing_helper(client, tmp_path):
    """Restart yang masih berjalan tidak boleh ditumpuk helper kedua."""
    journal = tmp_path / "restart-journal.json"
    journal.write_text(json.dumps({"state": "stopping", "stage": "stop"}), encoding="utf-8")
    body = client.post("/api/system/restart").json()
    assert body["already_running"] is True
    assert "argv" not in client.captured  # type: ignore[attr-defined]
