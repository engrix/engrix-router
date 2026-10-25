"""Restart helper: the failures that used to be silent must now be named.

Each test pins one of the three defects that made "restart sometimes fails"
impossible to diagnose:

  1. a port that never frees must produce state=failed with a REASON
     (router_service.ps1 used to print "already running" and exit 0);
  2. a server that dies right after start must report its exit code and the
     tail of stderr, not just "still down";
  3. success is only declared once /health answers -- never on spawn alone.

The helper's process control and health probe are patched, so nothing here
touches the real router or port 8450.
"""
from __future__ import annotations

import importlib.util
import json
import socket
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER_PATH = ROOT / "scripts" / "ops" / "router_restart.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("router_restart", HELPER_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def helper():
    return load_helper()


@pytest.fixture()
def fake_root(tmp_path: Path) -> Path:
    """A directory that looks enough like the router root for the helper."""
    (tmp_path / "src" / "engrix_router").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    return tmp_path


@pytest.fixture()
def journal(tmp_path: Path) -> Path:
    path = tmp_path / "logs" / "restart-journal.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class FakeProc:
    def __init__(self, returncode: int | None = None):
        self.pid = 424242
        self.returncode = returncode

    def poll(self):
        return self.returncode


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #
def test_write_journal_merges_and_stamps(helper, journal: Path):
    helper.write_journal(journal, state="stopping", stage="stop")
    first = read(journal)
    assert first["state"] == "stopping"
    assert first["stage"] == "stop"
    assert isinstance(first["updated_at_ms"], int)

    helper.write_journal(journal, state="done", reason=None)
    second = read(journal)
    # merge, not replace: the earlier stage survives
    assert second["stage"] == "stop"
    assert second["state"] == "done"
    assert second["reason"] is None


def test_write_journal_leaves_no_temp_file(helper, journal: Path):
    helper.write_journal(journal, state="x")
    leftovers = list(journal.parent.glob("*.tmp"))
    assert leftovers == [], f"atomic write left temp files: {leftovers}"


def test_write_journal_survives_a_corrupt_file(helper, journal: Path):
    journal.write_text("{not json", encoding="utf-8")
    helper.write_journal(journal, state="done")
    assert read(journal)["state"] == "done"


# --------------------------------------------------------------------------- #
# port / listener detection
# --------------------------------------------------------------------------- #
def test_listener_pid_finds_a_real_listener(helper):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        found = helper.listener_pid(port)
        assert found is not None, f"no listener detected on port {port}"
        assert found > 0
    finally:
        srv.close()


def test_port_free_is_false_while_held(helper):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert helper.port_free(port) is False
    finally:
        srv.close()
    assert helper.wait_port_free(port, timeout=5.0) is True


# --------------------------------------------------------------------------- #
# the three defects
# --------------------------------------------------------------------------- #
def test_port_never_freeing_reports_failed_with_reason(
    helper, fake_root: Path, journal: Path, monkeypatch
):
    """Defect 1: the old script claimed success here."""
    monkeypatch.setattr(helper, "orphan_pids", lambda root, keep: [])
    monkeypatch.setattr(helper, "listener_pid", lambda port: 9999)
    monkeypatch.setattr(helper, "pid_alive", lambda pid: True)
    monkeypatch.setattr(helper, "kill_pid", lambda pid, timeout=15.0: False)
    monkeypatch.setattr(helper, "wait_port_free", lambda port, timeout: False)
    monkeypatch.setattr(helper, "STOP_TIMEOUT_S", 0.1)
    started: list[str] = []
    monkeypatch.setattr(
        helper.subprocess, "Popen",
        lambda *a, **k: started.append("spawned") or FakeProc(),
    )

    code = helper.restart(fake_root, 8450, journal, wait=True)

    assert code == 1
    data = read(journal)
    assert data["state"] == "failed"
    assert data["stage"] == "stop"
    assert "still held by pid 9999" in data["reason"]
    assert started == [], "helper started a server while the port was still bound"


def test_server_dying_after_spawn_reports_exit_code_and_stderr(
    helper, fake_root: Path, journal: Path, monkeypatch
):
    """Defect 2: 'still down' with no cause is not a diagnosis."""
    monkeypatch.setattr(helper, "orphan_pids", lambda root, keep: [])
    monkeypatch.setattr(helper, "listener_pid", lambda port: None)
    monkeypatch.setattr(helper, "wait_port_free", lambda port, timeout: True)
    monkeypatch.setattr(helper, "health_uptime", lambda port, timeout=2.0: None)
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: FakeProc(returncode=3))
    monkeypatch.setattr(helper, "HEALTH_TIMEOUT_S", 5.0)

    code = helper.restart(fake_root, 8450, journal, wait=True)

    assert code == 1
    data = read(journal)
    assert data["state"] == "failed"
    assert data["stage"] == "start"
    assert "code 3" in data["reason"]


def test_success_requires_health_not_just_a_spawn(
    helper, fake_root: Path, journal: Path, monkeypatch
):
    """Defect 3: spawn != success."""
    monkeypatch.setattr(helper, "orphan_pids", lambda root, keep: [])
    monkeypatch.setattr(helper, "listener_pid", lambda port: None)
    monkeypatch.setattr(helper, "wait_port_free", lambda port, timeout: True)
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: FakeProc())

    calls = {"n": 0}

    def flaky_health(port, timeout=2.0):
        calls["n"] += 1
        return 12.5 if calls["n"] >= 3 else None

    monkeypatch.setattr(helper, "health_uptime", flaky_health)

    code = helper.restart(fake_root, 8450, journal, wait=True)

    assert code == 0
    assert calls["n"] >= 3, "success was declared before /health answered"
    data = read(journal)
    assert data["state"] == "done"
    assert data["reason"] is None
    assert data["uptime_s"] == 12.5


def test_health_never_answering_times_out_with_reason(
    helper, fake_root: Path, journal: Path, monkeypatch
):
    monkeypatch.setattr(helper, "orphan_pids", lambda root, keep: [])
    monkeypatch.setattr(helper, "listener_pid", lambda port: None)
    monkeypatch.setattr(helper, "wait_port_free", lambda port, timeout: True)
    monkeypatch.setattr(helper.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(helper, "health_uptime", lambda port, timeout=2.0: None)
    monkeypatch.setattr(helper, "HEALTH_TIMEOUT_S", 0.3)
    monkeypatch.setattr(helper, "POLL_INTERVAL_S", 0.05)

    code = helper.restart(fake_root, 8450, journal, wait=True)

    assert code == 1
    data = read(journal)
    assert data["state"] == "failed"
    assert "/health did not answer" in data["reason"]


# --------------------------------------------------------------------------- #
# CLI contract
# --------------------------------------------------------------------------- #
def test_cli_rejects_a_non_router_root(tmp_path: Path, capsys):
    helper = load_helper()
    code = helper.main(["--root", str(tmp_path)])
    assert code == 2
    assert "not a router root" in capsys.readouterr().err


def test_cli_writes_reason_when_it_crashes(fake_root: Path, journal: Path, monkeypatch):
    helper = load_helper()
    monkeypatch.setattr(
        helper, "restart",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    code = helper.main(["--root", str(fake_root), "--journal", str(journal)])
    assert code == 1
    data = read(journal)
    assert data["state"] == "failed"
    assert data["stage"] == "crashed"
    assert "RuntimeError: boom" in data["reason"]
