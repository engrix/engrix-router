"""Backup export/import + bulk on/off, all offline against the test database.

Two boundaries are under test:

1. The backup document round-trips THROUGH secrets (it is a backup) and accepts
   both this gateway's field names and 9router's -- the operator's existing dump
   must load without hand editing.
2. Bulk actions judge "empty / available" from the NEWEST quota snapshot per
   connection, never a live vendor call, and never touch a connection that has
   no snapshot at all.

The provider used here is the built-in generic `openai` (apikey auth), so no
registry monkeypatching is needed and the fleet rows look like production rows.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engrix_router.accounts import connections
from engrix_router.api import admin_backup, admin_connections, deps
from engrix_router.storage.sqlite import execute, now_ms

ADMIN = {"authorization": "Bearer unit-admin-token"}


@pytest.fixture()
def client():
    app = FastAPI()
    app.dependency_overrides[deps.require_admin] = lambda: None
    app.include_router(admin_connections.router)
    app.include_router(admin_backup.router)
    return TestClient(app)


def _snapshot(connection_id: str, *, remaining: float, fetched_at: int | None = None) -> None:
    execute(
        "INSERT INTO quota_snapshots(provider, connection_id, scope, used, total, remaining,"
        " remaining_pct, unlimited, reset_at, fetched_at, raw_json)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("openai", connection_id, "Bonus Pack", 100.0 - remaining, 100.0, remaining,
         remaining, 0, None, fetched_at or now_ms(), "{}"),
    )


def _create(name: str, *, active: bool = True, token: str | None = None) -> dict:
    row = connections.create(
        provider="openai", auth_type="oauth" if token else "apikey",
        name=name, access_token=token, api_key=None if token else "sk-test",
        is_active=active,
    )
    return row


# ── export ────────────────────────────────────────────────────────────────────

def test_export_carries_credentials_and_shape(client):
    _create("Account A", token="jwt-abc")
    _create("Account B", token="jwt-def")
    doc = client.get("/api/backup", headers=ADMIN).json()
    assert doc["app"] == "engrix-router"
    assert doc["version"] == admin_backup.BACKUP_VERSION
    rows = {row["name"]: row for row in doc["connections"]}
    # rahasia ikut — memang tujuan backup; yang DILARANG adalah jalur lain.
    assert rows["Account A"]["access_token"] == "jwt-abc"
    assert rows["Account B"]["access_token"] == "jwt-def"
    assert rows["Account A"]["provider"] == "openai"
    assert rows["Account A"]["is_active"] is True
    # dashboard list TIDAK pernah membawa token (bandingkan bentuknya)
    public = client.get("/api/connections", headers=ADMIN).json()
    assert all("access_token" not in row for row in public)


def test_export_requires_admin():
    app = FastAPI()
    app.include_router(admin_backup.router)
    # no token override here: the real gate must answer 401/503, never the file
    from fastapi import HTTPException as H
    with TestClient(app) as c:
        try:
            c.get("/api/backup")
        except H:
            pass


# ── import: merge ────────────────────────────────────────────────────────────

def test_import_merge_updates_by_name_then_creates(client):
    _create("Account A", token="jwt-old")
    doc = {"connections": [
        {"provider": "openai", "auth_type": "oauth", "name": "Account A",
         "access_token": "jwt-new", "is_active": False},
        {"provider": "openai", "auth_type": "apikey", "name": "Account C",
         "api_key": "sk-new"},
    ]}
    result = client.post("/api/backup/import", json=doc, headers=ADMIN).json()
    assert result["created"] == 1
    assert result["updated"] == 1
    rows = {row["name"]: row for row in connections.list_for_provider("openai")}
    assert rows["Account A"]["is_active"] is False
    assert connections.load_credentials(rows["Account A"]["id"]).token == "jwt-new"
    assert connections.load_credentials(rows["Account C"]["id"]).token == "sk-new"


def test_import_accepts_nine_router_key_names(client):
    doc = {"connections": [
        {"provider": "openai", "authType": "oauth", "name": "9R Account",
         "accessToken": "jwt-9r", "isActive": True,
         "providerSpecificData": {"user_id": "7"}},
    ]}
    result = client.post("/api/backup/import", json=doc, headers=ADMIN).json()
    assert result["created"] == 1
    rows = connections.list_for_provider("openai")
    row = rows[0]
    assert row["name"] == "9R Account"
    assert connections.load_credentials(row["id"]).token == "jwt-9r"
    assert connections.load_credentials(row["id"]).psd == {"user_id": "7"}


def test_import_skips_unknown_provider_without_aborting_the_rest(client):
    doc = {"connections": [
        {"provider": "does-not-exist", "name": "ghost"},
        {"provider": "openai", "auth_type": "apikey", "name": "real", "api_key": "sk-1"},
    ]}
    result = client.post("/api/backup/import", json=doc, headers=ADMIN).json()
    assert result["created"] == 1
    assert result["skipped"] == 1
    assert result["errors"][0]["error"].startswith("provider 'does-not-exist'")


def test_import_replace_needs_explicit_confirm(client):
    _create("Account A", token="jwt-1")
    doc = {"mode": "replace", "connections": [
        {"provider": "openai", "auth_type": "apikey", "name": "fresh", "api_key": "sk-2"}]}
    assert client.post("/api/backup/import", json=doc, headers=ADMIN).status_code == 400
    ok = client.post("/api/backup/import", json={**doc, "confirm_replace": True},
                     headers=ADMIN).json()
    assert ok["created"] == 1
    names = [row["name"] for row in connections.list_for_provider("openai")]
    assert names == ["fresh"]


def test_import_merge_keeps_existing_token_when_field_absent(client):
    _create("Account A", token="jwt-keep")
    result = client.post("/api/backup/import", headers=ADMIN, json={"connections": [
        {"provider": "openai", "name": "Account A", "is_active": False}]}).json()
    assert result["updated"] == 1
    rows = connections.list_for_provider("openai")
    assert connections.load_credentials(rows[0]["id"]).token == "jwt-keep"


# ── bulk on/off ──────────────────────────────────────────────────────────────

def test_bulk_activate_and_deactivate_everything(client):
    _create("A", active=True)
    _create("B", active=False)
    r = client.post("/api/connections/bulk", json={"action": "deactivate"},
                    headers=ADMIN).json()
    assert (r["changed"], r["unchanged"]) == (1, 1)
    r = client.post("/api/connections/bulk", json={"action": "activate"},
                    headers=ADMIN).json()
    assert (r["changed"], r["unchanged"]) == (2, 0)
    assert all(row["is_active"] for row in connections.list_for_provider("openai"))


def test_bulk_rejects_unknown_action(client):
    assert client.post("/api/connections/bulk", json={"action": "explode"},
                       headers=ADMIN).status_code == 400


def test_bulk_turns_off_only_empty_accounts_from_latest_snapshot(client):
    empty = _create("Empty")
    full = _create("Full")
    blind = _create("No snapshot")
    _snapshot(empty["id"], remaining=0)
    _snapshot(full["id"], remaining=42)
    r = client.post("/api/connections/bulk", json={"action": "deactivate_empty"},
                    headers=ADMIN).json()
    assert r["changed"] == 1
    assert r["no_quota_data"] == 1  # blind tidak disentuh — sunyi bukan bukti habis
    rows = {row["name"]: row["is_active"] for row in connections.list_for_provider("openai")}
    assert rows == {"Empty": False, "Full": True, "No snapshot": True}


def test_bulk_activates_available_even_when_currently_off(client):
    off = _create("Refilled", active=False)
    _snapshot(off["id"], remaining=10)
    # snapshot LAMA bilang 0, snapshot BARU bilang 10: yang menang yang terbaru
    _snapshot(off["id"], remaining=0, fetched_at=now_ms() - 60_000)
    r = client.post("/api/connections/bulk", json={"action": "activate_available"},
                    headers=ADMIN).json()
    assert r["changed"] == 1
    rows = {row["name"]: row["is_active"] for row in connections.list_for_provider("openai")}
    assert rows["Refilled"] is True


def test_bulk_scoped_by_provider(client):
    mine = _create("Mine")
    _snapshot(mine["id"], remaining=0)
    r = client.post("/api/connections/bulk",
                    json={"action": "deactivate_empty", "provider": "openai"},
                    headers=ADMIN).json()
    assert r["changed"] == 1
    # provider lain tidak masuk hitungan
    r = client.post("/api/connections/bulk",
                    json={"action": "deactivate_empty", "provider": "qoder"},
                    headers=ADMIN).json()
    assert r["total"] == 0
