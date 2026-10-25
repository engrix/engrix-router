"""Token introspection at ingest: every field a pasted OAuth blob carries must land.

The failure this guards against is silent, which is why it survived so long: an
OAuth connection was stored with the raw token string and nothing else, so
`expires_at` stayed NULL on every row. `Credentials.is_expired` returns False when
`expires_at` is empty, so the expiry check could never fire -- the router had no
way to tell a live token from a dead one except by waiting for the vendor to say
no. The same ingest also dropped the account id the desktop client glues onto the
token, and the identity claims (sub/iat/exp/scope) that make "why did this account
stop working" answerable later.

Three properties are asserted here, each of which was broken before:

1. `expires_at` is derived from the token's own `exp` claim at create AND update.
2. The `<account-id>+<jwt>` composite form is split, because sending it whole is
   refused by the vendor as a malformed bearer credential.
3. An opaque value that is not a JWT is stored untouched -- an API key must not be
   mangled by a token parser.
"""
from __future__ import annotations

import base64
import json

import pytest

from engrix_router.core import tokens


def _make_jwt(claims: dict) -> str:
    """A structurally valid JWT. The signature is irrelevant: nothing verifies it."""
    def segment(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{segment({'alg': 'RS256', 'typ': 'JWT'})}.{segment(claims)}.signature"


SUBJECT = "2f729e0e-3f7f-4f0f-9f21-cca8a1b55e5a"


@pytest.fixture()
def sample_claims() -> dict:
    return {
        "sub": SUBJECT,
        "iat": 1792949183,
        "exp": 1824485179,
        "azp": "console",
        "aud": "account",
        "iss": "https://www.codebuddy.ai",
        "scope": "openid profile",
        "jti": "abc-123",
        "typ": "Bearer",
    }


# ── claim decoding ───────────────────────────────────────────────────────────────
def test_decode_claims_reads_the_payload(sample_claims):
    assert tokens.decode_claims(_make_jwt(sample_claims))["sub"] == SUBJECT


def test_decode_claims_returns_empty_for_an_api_key():
    """An opaque key is not a token. Returning {} keeps it storable."""
    assert tokens.decode_claims("sk-proj-abc123") == {}
    assert tokens.decode_claims("") == {}
    assert tokens.decode_claims(None) == {}


def test_decode_claims_survives_a_malformed_payload():
    assert tokens.decode_claims("a.!!!not-base64!!!.c") == {}


def test_subject_and_expiry_are_iso_utc(sample_claims):
    claims = tokens.decode_claims(_make_jwt(sample_claims))
    assert tokens.subject(claims) == SUBJECT
    assert tokens.expiry_iso(claims) == "2027-10-25T17:26:19Z"
    assert tokens.issued_iso(claims) == "2026-10-25T17:26:23Z"


def test_expiry_accepts_milliseconds():
    """A vendor emitting `exp` in ms must not be read as the year 58000."""
    assert tokens.epoch_to_iso(1824485179000) == tokens.epoch_to_iso(1824485179)


# ── composite form ──────────────────────────────────────────────────────────────
def test_composite_is_split_into_account_id_and_token(sample_claims):
    jwt = _make_jwt(sample_claims)
    account_id, bare = tokens.split_composite(f"{SUBJECT}+{jwt}")
    assert account_id == SUBJECT
    assert bare == jwt


def test_plain_token_is_not_split():
    jwt = _make_jwt({"sub": SUBJECT})
    assert tokens.split_composite(jwt) == (None, jwt)
    assert tokens.split_composite("sk-plain-key") == (None, "sk-plain-key")


def test_a_plus_sign_inside_a_non_jwt_value_is_left_alone():
    """Only split when the tail is actually a JWT -- an API key may contain '+.'"""
    assert tokens.split_composite("a+b+c") == (None, "a+b+c")


def test_normalise_drops_the_prefix_and_returns_claims(sample_claims):
    jwt = _make_jwt(sample_claims)
    bare, claims = tokens.normalise(f"{SUBJECT}+{jwt}")
    assert bare == jwt
    assert claims["exp"] == sample_claims["exp"]


# ── what a dashboard may read ───────────────────────────────────────────────────
def test_public_summary_keeps_identity_and_drops_secrets(sample_claims):
    summary = tokens.public_summary(sample_claims)
    assert summary["sub"] == SUBJECT
    assert summary["scope"] == "openid profile"
    # The token material itself is never part of a claim summary.
    assert "access_token" not in summary
    assert "signature" not in summary


# ── ingest: what actually lands in the database ─────────────────────────────────
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from engrix_router.accounts import connections  # noqa: E402
from engrix_router.api import admin_connections, deps  # noqa: E402
from engrix_router.storage.sqlite import execute  # noqa: E402


def test_create_derives_expires_at_from_the_token(sample_claims):
    """The original bug: `expires_at` stayed NULL on every OAuth connection."""
    row = connections.create(
        provider="openai", auth_type="oauth", name="oauth-acc",
        access_token=_make_jwt(sample_claims),
    )
    stored = connections.load_credentials(row["id"])
    assert stored.expires_at == "2027-10-25T17:26:19Z"
    assert stored.is_expired is False


def test_create_splits_the_composite_bearer(sample_claims):
    """The desktop client stores `<account-id>+<jwt>`; the vendor refuses that whole."""
    jwt = _make_jwt(sample_claims)
    row = connections.create(
        provider="openai", auth_type="oauth", name="composite",
        access_token=f"{SUBJECT}+{jwt}",
    )
    stored = connections.load_credentials(row["id"])
    assert stored.token == jwt
    assert stored.psd["uid"] == SUBJECT


def test_create_records_the_identity_claims(sample_claims):
    row = connections.create(
        provider="openai", auth_type="oauth", name="claims",
        access_token=_make_jwt(sample_claims),
    )
    view = connections.get_for_dashboard(row["id"])
    psd = view["provider_specific"]
    assert psd["uid"] == SUBJECT
    assert psd["token_issued_at"] == "2026-10-25T17:26:23Z"
    assert psd["token_claims"]["scope"] == "openid profile"
    # The credential itself never appears in a dashboard view.
    assert "access_token" not in view


def test_create_leaves_an_api_key_untouched():
    """A parser must not mangle an opaque key: no claims, no derived expiry."""
    row = connections.create(provider="openai", name="keyed", api_key="sk-proj-abc")
    stored = connections.load_credentials(row["id"])
    assert stored.token == "sk-proj-abc"
    assert stored.expires_at is None


def test_operator_expires_at_wins_over_the_claim(sample_claims):
    row = connections.create(
        provider="openai", auth_type="oauth", name="explicit",
        access_token=_make_jwt(sample_claims),
        expires_at="2030-01-01T00:00:00Z",
    )
    assert connections.load_credentials(row["id"]).expires_at == "2030-01-01T00:00:00Z"


def test_update_repastes_the_token_and_refreshes_the_expiry(sample_claims):
    """`Edit -> paste a new token` must not keep the stale expiry from the old one."""
    row = connections.create(
        provider="openai", auth_type="oauth", name="rotating",
        access_token=_make_jwt({"sub": SUBJECT, "exp": 1000000000}),
    )
    assert connections.load_credentials(row["id"]).expires_at == "2001-09-09T01:46:40Z"
    connections.update(row["id"], {"access_token": _make_jwt(sample_claims)})
    stored = connections.load_credentials(row["id"])
    assert stored.expires_at == "2027-10-25T17:26:19Z"
    assert stored.is_expired is False


def test_legacy_rows_without_expires_at_are_still_read(sample_claims):
    """Rows imported from 9router carry no expires_at; the token answers anyway."""
    row = connections.create(
        provider="openai", auth_type="oauth", name="legacy",
        access_token=_make_jwt(sample_claims),
    )
    execute("UPDATE connections SET cred_json = ? WHERE id = ?",
            (json.dumps({"access_token": _make_jwt(sample_claims)}), row["id"]))
    stored = connections.load_credentials(row["id"])
    assert stored.expires_at == "2027-10-25T17:26:19Z"
    view = connections.get_for_dashboard(row["id"])
    assert view["expires_at"] == "2027-10-25T17:26:19Z"


# ── paste path: an unknown field must not be dropped silently ───────────────────
@pytest.fixture()
def admin_client():
    app = FastAPI()
    app.dependency_overrides[deps.require_admin] = lambda: None
    app.include_router(admin_connections.router)
    return TestClient(app)


def _paste(admin_client, **extra) -> dict:
    """POST a full OAuth payload the way an operator pastes one."""
    body = {
        "provider": "openai",
        "auth_type": "oauth",
        "name": "pasted",
        "access_token": _make_jwt({"sub": SUBJECT, "exp": 1824485179}),
        "probe": False,
    }
    body.update(extra)
    response = admin_client.post("/api/connections", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_unknown_oauth_fields_are_kept_in_provider_specific(admin_client):
    """`id_token`, `token_type`, `scope`... used to vanish without a trace."""
    row = _paste(admin_client, id_token="header.payload.signature",
                 token_type="Bearer", scope="openid profile", account_id="acc-1")
    psd = row["provider_specific"]
    assert psd["id_token"] == "header.payload.signature"
    assert psd["token_type"] == "Bearer"
    assert psd["scope"] == "openid profile"
    assert psd["account_id"] == "acc-1"


def test_known_fields_do_not_leak_into_provider_specific(admin_client):
    row = _paste(admin_client, priority=7)
    psd = row["provider_specific"]
    for key in ("provider", "name", "access_token", "probe", "priority", "auth_type"):
        assert key not in psd
    assert row["priority"] == 7


def test_explicit_provider_specific_wins_over_a_top_level_duplicate(admin_client):
    """The nested block is the operator's deliberate choice; it must not be overwritten."""
    row = _paste(admin_client, provider_specific={"region": "intl"}, region="cn")
    assert row["provider_specific"]["region"] == "intl"


def test_patch_keeps_unknown_fields_too(admin_client):
    row = _paste(admin_client)
    response = admin_client.patch(f"/api/connections/{row['id']}",
                                 json={"id_token": "a.b.c", "note": "renewed"})
    assert response.status_code == 200, response.text
    psd = response.json()["provider_specific"]
    assert psd["id_token"] == "a.b.c"
    assert psd["note"] == "renewed"
