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

import json

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


# ------------------------------------------- the route still streams at all


@pytest.fixture(autouse=True)
def _clean_limiter():
    """These tests drive the whole route, so the per-provider window is shared state.

    Without this the second request in the same minute is refused with 429 and the
    test reads as a routing bug rather than the limiter doing its job.
    """
    from engrix_router.hooks import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture
def client_key():
    from engrix_router.identity import api_keys

    created = api_keys.create_key("stream-route-test")
    return {"authorization": f"Bearer {created['key']}"}


@pytest.fixture
def connection():
    """Routing needs a candidate: with no connection the route answers 503 by design."""
    from engrix_router.accounts import connections

    return connections.create(provider="anthropic", name="stream-acct", api_key="sk-test")


def test_chat_completions_route_actually_streams(monkeypatch, client_key, connection):
    """The peek must not have cost the route its renderer.

    Regression: the route was changed from `runner.stream_chat(...)` to a direct
    `runner.stream_frames(...)` to be able to peek the first frame, and the
    `render=` argument came with it. `stream_frames` requires it, so EVERY
    streaming request died with a TypeError before the first frame existed and
    the client got "internal gateway error" -- the peek never even ran. Nothing
    below the route was exercised by the tests that shipped with that change.
    """
    from fastapi.testclient import TestClient

    from engrix_router.core.formats import openai as openai_fmt
    from engrix_router.providers import anthropic as anthropic_provider
    from engrix_router.web import server

    async def fake_stream(self, request):
        for delta, finish in (({"role": "assistant", "content": "HEL"}, None),
                              ({"content": "LO"}, None),
                              ({}, "stop")):
            yield openai_fmt.chunk(rid="r", model="claude-x", created=1,
                                   delta=delta, finish_reason=finish)

    monkeypatch.setattr(anthropic_provider.AnthropicProvider, "open_stream", fake_stream)
    with TestClient(server.app) as client:
        with client.stream("POST", "/v1/chat/completions", headers=client_key, json={
                "model": "anthropic/claude-x", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]}) as response:
            assert response.status_code == 200
            raw = "".join(response.iter_text())

    assert "internal gateway error" not in raw
    assert raw.rstrip().endswith("data: [DONE]"), raw
    payloads = [json.loads(line[5:]) for line in raw.split("\n")
                if line.startswith("data:") and "[DONE]" not in line]
    assert [p["object"] for p in payloads] == ["chat.completion.chunk"] * len(payloads)
    assert "".join(p["choices"][0]["delta"].get("content") or "" for p in payloads) == "HELLO"
    # trailer frame: finish_reason must reach the client, or its SDK reads a stall
    assert payloads[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_one_renderer_feeds_both_the_socket_and_the_trace():
    """`render=` exists so the trace stores the bytes that were sent, not a re-render.

    That only holds if both sides call the same function -- which is why openai_frame()
    is one function instead of a branch inside sse_frames(). The error frame is the
    case that used to disagree: the socket got error_frame() + [DONE] while the trace
    recorded the raw json.
    """
    from engrix_router.core.formats import openai as openai_fmt

    ok = [{"choices": [{"index": 0, "delta": {"content": "hi"}}]}]
    sent = "".join([chunk async for chunk in runner.sse_frames(_frames(*ok))])
    assert sent == "".join(runner.openai_frame(frame) for frame in ok) + openai_fmt.SSE_DONE

    failed = [{"__error__": True, "message": "vendor said no",
               "code": errors.CLASS_UPSTREAM_UNAVAILABLE, "status": 502,
               "note": "gave up after 3 accounts"}]
    sent = "".join([chunk async for chunk in runner.sse_frames(_frames(*failed))])
    assert sent == "".join(runner.openai_frame(frame) for frame in failed)
    assert sent.rstrip().endswith("data: [DONE]") and sent.count("[DONE]") == 1


def test_stream_that_dies_before_the_first_frame_keeps_its_status(client_key):
    """No connection at all: the status must be 503, not 200 with an error inside.

    This is the failure commit 88c39b2 set out to fix, and it only works because
    the peek happens before StreamingResponse exists. Deliberately does NOT ask for
    the `connection` fixture -- an empty fleet is the case under test.
    """
    from fastapi.testclient import TestClient

    from engrix_router.web import server

    with TestClient(server.app) as client:
        response = client.post("/v1/chat/completions", headers=client_key, json={
            "model": "anthropic/claude-x", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"]["code"] == errors.CLASS_ALL_LOCKED
    assert "[DONE]" not in response.text


