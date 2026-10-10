"""OAuth+ link flow: core state machine with a mocked provider (zero network).

The point of these tests is the boundary: core owns sessions, gating
(category + admin token), persistence (one connections row per successful
link) and rate-limiting -- the provider hook owns every vendor fact. A fake
provider proves that contract without importing any private package.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from engrix_router.accounts import connections
from engrix_router.api import admin_oauth, deps
from engrix_router.core.types import ProviderDef, TransportSpec
from engrix_router.providers import registry

ADMIN = {"authorization": "Bearer unit-admin-token"}


class FakeProvider:
    def __init__(self) -> None:
        self.poll_calls = 0
        self.start_calls = 0

    async def oauth_start(self) -> dict:
        self.start_calls += 1
        return {"authorize_url": "https://vendor.example/authorize?x=1",
                "flow_token": "flow-abc", "poll_interval_sec": 5}

    async def oauth_poll(self, flow_token: str) -> dict:
        self.poll_calls += 1
        assert flow_token == "flow-abc"
        if self.poll_calls == 1:
            return {"status": "pending"}
        return {"status": "ready", "account": {
            "access_token": "jwt-token", "api_key": "key-2", "refresh_token": "r-1",
            "email": "a@b.c", "name": "Account A",
            "provider_specific": {"user_id": "42", "device_mid": "mid-1"}}}


def _definition(category: str = "oauth") -> ProviderDef:
    return ProviderDef(
        id="fake-oauth", category=category, display_name="Fake",
        auth_modes=("oauth",),
        transport=TransportSpec(base_url="https://vendor.example"))


@pytest.fixture()
def app(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(registry, "get_definition", lambda _pid: _definition())
    monkeypatch.setattr(registry, "get_provider", lambda _pid: provider)
    admin_oauth._SESSIONS.clear()
    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[deps.require_admin] = lambda: None
    fastapi_app.include_router(admin_oauth.router)
    yield fastapi_app, provider
    admin_oauth._SESSIONS.clear()


def test_start_returns_vendor_link_and_creates_session(app):
    client = TestClient(app[0])
    started = client.post("/api/oauth/fake-oauth/start").json()
    assert started["authorize_url"] == "https://vendor.example/authorize?x=1"
    assert started["poll_interval_sec"] == 5
    assert started["session_id"]
    assert started["expires_at"] > started["expires_at"] - 10 ** 9  # sanity: epoch-ms depan


def test_poll_state_machine_pending_then_ready_persists_connection(app):
    client = TestClient(app[0])
    started = client.post("/api/oauth/fake-oauth/start").json()
    sid = started["session_id"]
    assert client.get(f"/api/oauth/fake-oauth/{sid}").json() == {"status": "pending"}
    # # Rate limit lokal: poll susulan langsung kena jeda vendor-interval,
    # # hook gak boleh dipanggil dua kali beruntun.
    throttled = client.get(f"/api/oauth/fake-oauth/{sid}").json()
    assert throttled["status"] == "pending" and "retry_after_ms" in throttled
    assert app[1].poll_calls == 1
    # # Siap-ntuk-baca: paksa jeda lewat supaya bisa nanya lagi.
    admin_oauth._SESSIONS[sid]["poll_after_ms"] = 0
    done = client.get(f"/api/oauth/fake-oauth/{sid}").json()
    assert done["status"] == "ready" and done["connection_id"]
    row = connections.get_for_dashboard(done["connection_id"])
    assert row["auth_type"] == "oauth" and row["email"] == "a@b.c"
    creds = connections.load_credentials(done["connection_id"])
    assert creds.token == "jwt-token"
    # # Sesi sekali-pakai: poll ulang 404, bukan dobel koneksi.
    assert client.get(f"/api/oauth/fake-oauth/{sid}").status_code == 404
    assert len(connections.list_for_provider("fake-oauth")) == 1


def test_ready_without_token_fails_without_touching_db(app, monkeypatch):
    provider = app[1]

    async def broken_poll(_flow_token: str) -> dict:
        return {"status": "ready", "account": {"name": "no-token"}}

    monkeypatch.setattr(provider, "oauth_poll", broken_poll)
    client = TestClient(app[0])
    sid = client.post("/api/oauth/fake-oauth/start").json()["session_id"]
    result = client.get(f"/api/oauth/fake-oauth/{sid}").json()
    assert result["status"] == "failed" and "access token" in result["error"]
    assert connections.list_for_provider("fake-oauth") == []


def test_apikey_category_provider_is_refused(monkeypatch):
    monkeypatch.setattr(registry, "get_definition", lambda _pid: _definition("apikey"))
    monkeypatch.setattr(registry, "get_provider", lambda _pid: FakeProvider())
    admin_oauth._SESSIONS.clear()
    fastapi_app = FastAPI()
    from engrix_router.api import deps as deps_mod
    fastapi_app.dependency_overrides[deps_mod.require_admin] = lambda: None
    fastapi_app.include_router(admin_oauth.router)
    client = TestClient(fastapi_app)
    assert client.post("/api/oauth/fake-oauth/start").status_code == 409


def test_endpoints_require_admin_token():
    # # Tanpa override: guard asli = 401, OAuth+ gak jadi pintu belakang.
    fastapi_app = FastAPI()
    fastapi_app.include_router(admin_oauth.router)
    client = TestClient(fastapi_app)
    assert client.post("/api/oauth/fake-oauth/start").status_code == 401


def test_unknown_provider_is_404(monkeypatch):
    monkeypatch.setattr(registry, "get_definition", lambda _pid: None)
    fastapi_app = FastAPI()
    fastapi_app.dependency_overrides[deps.require_admin] = lambda: None
    fastapi_app.include_router(admin_oauth.router)
    client = TestClient(fastapi_app)
    assert client.post("/api/oauth/ghost/start").status_code == 404


def test_dashboard_wires_the_oauth_button_and_core_mounts_the_router():
    # # Dua hal yang bikin tombol "OAuth+" janji bukan teori: (1) halaman
    # # SUNGGUHAN merender button.oauth + memanggil endpoint start/poll,
    # # (2) router-ke-mounter di app server asli dengan guard admin — tanpa
    # # token: 401; dengan token: 404 provider ghost (bukan 405, route exist).
    import pathlib

    html = (pathlib.Path(admin_oauth.__file__).resolve().parents[1]
            / "web" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'button class="tiny primary oauth"' in html or "button.oauth" in html
    # TASK-42: tab Connections digabung jadi hub Providers -- tombol OAuth+
    # pindah dari tabel ke kartu provider (class "tiny primary oauth").
    assert "/api/oauth/${encodeURIComponent(b.dataset.provider)}/start" in html
    assert "conn.oauth_waiting" in html and "conn.oauth_linked" in html

    from engrix_router.web import server
    with TestClient(server.app) as client:
        assert client.post("/api/oauth/ghost/start").status_code == 401
        ok = client.post("/api/oauth/ghost/start", headers=ADMIN)
        assert ok.status_code == 404  # route ada, provider-nya yang nggak ada
