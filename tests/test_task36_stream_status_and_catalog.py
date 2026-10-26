"""Honesty rules on the /v1 data path.

Two failures were measured against the running gateway, and both made the client
believe something that was not true:

  * a stream that failed before producing any content answered HTTP 200, so an
    SDK read "success with an empty answer" and the real cause (no healthy
    connection) never reached it;
  * /v1/models advertised `cline/...` while cline's only token was expired, so
    the client was sent into a guaranteed failure it could not avoid.

These tests pin the decision, not the plumbing: the peek must not lose or
duplicate a frame, the failure must be the SAME class the non-stream path
reports, and a provider with no usable connection must not be listed.
"""
from __future__ import annotations

import pytest

from engrix_router.api import openai as openai_api
from engrix_router.core import errors
from engrix_router.pipeline import runner


def _frames(*items):
    async def generate():
        for item in items:
            yield item

    return generate()


# --------------------------------------------------------------- head_frame


@pytest.mark.asyncio
async def test_head_frame_replays_the_peeked_frame_exactly_once():
    """The peek must not swallow or duplicate the first frame."""
    first = {"choices": [{"delta": {"content": "A"}}]}
    second = {"choices": [{"delta": {"content": "B"}}]}
    head, frames = await runner.head_frame(_frames(first, second))

    assert head is first
    replayed = [frame async for frame in frames]
    assert replayed == [first, second], "first frame must appear once, in order"


@pytest.mark.asyncio
async def test_head_frame_on_empty_stream_returns_none():
    head, frames = await runner.head_frame(_frames())
    assert head is None
    assert [frame async for frame in frames] == []


# --------------------------------------------------- classified_from_frame


def test_error_frame_class_uses_the_same_decision_as_the_non_stream_path():
    """`all_locked` must survive the round-trip, not degrade to upstream_unavailable."""
    frame = {"__error__": True, "message": "no healthy connection: token expired",
             "code": errors.CLASS_ALL_LOCKED, "status": 503,
             "upstream_status": None, "vendor_code": None}
    classified = runner.classified_from_frame(frame)
    assert classified.error_class == errors.CLASS_ALL_LOCKED
    assert classified.client_status == 503


def test_error_frame_with_unknown_class_falls_back_to_classify():
    frame = {"__error__": True, "message": "boom", "code": "not_a_class",
             "status": 502, "vendor_code": None}
    classified = runner.classified_from_frame(frame)
    assert classified.error_class in errors.ALL_CLASSES


# ------------------------------------------------------------ catalog filter


def test_is_usable_rejects_blocked_expired_and_credential_less_rows(monkeypatch):
    """Three independent reasons a row cannot serve a request."""
    from engrix_router.accounts import health as health_mod
    from engrix_router.accounts.connections import Credentials

    def creds(*, token="t", expired=False, auth_type="oauth"):
        return Credentials(
            connection_id="c1", provider="codebuddy", auth_type=auth_type, name="n",
            token=token, refresh_token=None, expires_at="2000-01-01T00:00:00Z" if expired else None,
            psd={}, proxy_url=None, proxy_strict=False,
        )

    monkeypatch.setattr(openai_api.connections, "load_credentials", lambda _id: creds())

    assert openai_api._is_usable({"id": "c1", "health": {"test_status": health_mod.STATUS_ACTIVE}}) is True

    for blocked in health_mod.BLOCKING_STATUSES:
        assert openai_api._is_usable({"id": "c1", "health": {"test_status": blocked}}) is False, blocked

    monkeypatch.setattr(openai_api.connections, "load_credentials", lambda _id: None)
    assert openai_api._is_usable({"id": "c1", "health": {"test_status": health_mod.STATUS_ACTIVE}}) is False

    monkeypatch.setattr(openai_api.connections, "load_credentials", lambda _id: creds(token=""))
    assert openai_api._is_usable({"id": "c1", "health": {"test_status": health_mod.STATUS_ACTIVE}}) is False

    monkeypatch.setattr(openai_api.connections, "load_credentials", lambda _id: creds(expired=True))
    assert openai_api._is_usable({"id": "c1", "health": {"test_status": health_mod.STATUS_ACTIVE}}) is False


def test_is_usable_allows_a_row_without_health_record():
    """No health row means "never used", not "broken" -- it must still be offered."""
    from engrix_router.accounts import health as health_mod
    from engrix_router.accounts.connections import Credentials

    original = openai_api.connections.load_credentials
    try:
        openai_api.connections.load_credentials = lambda _id: Credentials(
            connection_id="c1", provider="codebuddy", auth_type="oauth", name="n",
            token="t", refresh_token=None, expires_at=None, proxy_url=None,
            proxy_strict=False, psd={})
        assert openai_api._is_usable({"id": "c1", "health": {}}) is True
        assert openai_api._is_usable({"id": "c1"}) is True
        # STATUS_UNKNOWN is not a blocking status, so it stays offered
        assert openai_api._is_usable(
            {"id": "c1", "health": {"test_status": health_mod.STATUS_UNKNOWN}}) is True
    finally:
        openai_api.connections.load_credentials = original
