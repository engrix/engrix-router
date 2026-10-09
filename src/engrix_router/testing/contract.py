"""
Shared contract test suite for providers (ADR-0002).

The public core owns the contract; every provider -- built-in here, or a private
distribution attached through an entry point -- has to satisfy the same suite. That is
what keeps "public core" honest: the invariants are not prose in a README, they are
tests a vendor adapter cannot pass by accident.

Usage in a provider repository (or in `tests/contract/` here):

    from engrix_router.testing.contract import ProviderContract

    class TestMyVendor(ProviderContract):
        provider_id = "myvendor"
        model = "myvendor/model-x"
        ...

The class is not named ``Test*`` on purpose: pytest must not collect the base class
itself, only the subclasses that supply the fixtures.
"""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from engrix_router.accounts import connections
from engrix_router.core.types import Credentials, ModelSpec, ProbeResult, UpstreamError
from engrix_router.core.formats import openai as openai_fmt
from engrix_router.providers import registry
from engrix_router.transport import http_client

VALID_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "error", None}


def stub_client(*, body: str, status: int = 200, content_type: str = "text/event-stream") -> httpx.AsyncClient:
    """One canned answer for any request -- enough to exercise a provider offline."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body.encode("utf-8"),
                              headers={"content-type": content_type})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class ProviderContract:
    """Everything a provider must be able to do, proven without touching the network."""

    provider_id: str = ""
    model: str = ""                       # `provider/model` utuh, persis kayak yang dikirim klien
    chat_body: str = "{}"                 # jawaban upstream non-stream
    stream_text: str = ""                 # byte SSE mentah (baris data:) buat stream
    models_body: str = '{"data": []}'
    chat_content_type: str = "application/json"   # vendor yang cuma mau stream nge-override ini
    error_status: int = 429
    error_body: str = '{"error": {"message": "slow down"}}'
    expected_content: str = "HELLO"
    expected_usage_total: int = 0
    probe_tier: str | None = None         # None = jangan klaim tier tertentu
    # Default = bentuk wire OpenAI; vendor nge-override byte-nya, bukan assertion-nya.
    tool_stream: str = "".join(f"data: {json.dumps(frame)}\n\n" for frame in (
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "add", "arguments": '{"a":'}}]}}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": "1}"}}]}}]}))
    expected_tool_name: str = "add"
    expected_arguments: dict = {"a": 1}
    # Apa yang masuk ke `connections.create` -- vendor oauth override ini buat
    # access_token + providerSpecificData yang dibutuh signing-nya.
    connection_kwargs: dict = {"api_key": "sk-contract"}

    # ── plumbing ─────────────────────────────────────────────────────────────
    @pytest.fixture
    def provider(self):
        assert self.provider_id, "a provider contract must set provider_id"
        return registry.get_provider(self.provider_id)

    @pytest.fixture
    def connection(self):
        """A REAL connection row: catalog/health tables have FKs to it, and signing
        adapters need its providerSpecificData. A fake dict proves nothing."""
        row = connections.create(provider=self.provider_id, name="contract-account",
                                 **self.connection_kwargs)
        yield row
        connections.delete(row["id"])

    @pytest.fixture
    def creds(self, connection) -> Credentials:
        """
        Credentials are loaded back through the real path, not hand-built.

        A hand-made dict would skip `providerSpecificData` -- and a signing adapter
        (COSY-style) needs exactly that: user id and machine id live there.
        """
        creds = connections.load_credentials(connection["id"])
        assert creds is not None, "the connection row was created but has no credentials"
        return creds

    @pytest.fixture
    def no_network(self, monkeypatch):
        """Any HTTP the provider does goes through this stub; nothing leaves the process."""
        state: dict[str, Any] = {}

        async def fake_get_client(proxy_url: str | None = None) -> httpx.AsyncClient:
            return stub_client(**state)

        monkeypatch.setattr(http_client, "get_client", fake_get_client)
        return state

    def _serve(self, state: dict[str, Any], **kwargs) -> None:
        state.clear()
        state.update(kwargs)

    # ── 1. bangun request itu murni ─────────────────────────────────────────
    def test_build_request_is_offline_pure_and_does_not_mutate_the_body(self, provider, creds):
        body = {"model": self.model, "messages": [{"role": "user", "content": "hi"}]}
        before = json.dumps(body, sort_keys=True)
        request = provider.build_request(dict(body), creds, stream=False)
        assert request.url.startswith("http")
        assert isinstance(request.body, dict)
        assert request.body_bytes is None or isinstance(request.body_bytes, bytes)
        assert json.dumps(body, sort_keys=True) == before, "transform mutated the caller's body"

    def test_two_builds_do_not_share_mutable_state(self, provider, creds):
        """Provider instances are singletons; per-request state must not live on self."""
        body = {"model": self.model, "messages": [{"role": "user", "content": "one"}]}
        first = provider.build_request(dict(body), creds, stream=True)
        body["messages"][0]["content"] = "two"
        second = provider.build_request(dict(body), creds, stream=True)
        assert first.body is not second.body
        assert first.body_bytes != second.body_bytes or first.body != second.body

    # ── 2. stream didekode jadi frame kanonik ──────────────────────────────
    async def test_open_stream_yields_canonical_frames(self, provider, creds, no_network):
        self._serve(no_network, body=self.stream_text)
        request = provider.build_request({"model": self.model, "stream": True,
                                          "messages": [{"role": "user", "content": "hi"}]},
                                         creds, stream=True)
        frames = [frame async for frame in provider.open_stream(request)]
        assert frames, "the provider produced no frames at all"
        for frame in frames:
            assert isinstance(frame, dict)
            assert frame.get("object") in (None, "chat.completion.chunk"), frame
            for choice in frame.get("choices") or []:
                assert isinstance(choice.get("delta"), dict), choice
                assert choice.get("finish_reason") in VALID_FINISH_REASONS, choice
        aggregated = openai_fmt.aggregate_stream_text(frames)
        assert aggregated["content"] == self.expected_content
        if self.expected_usage_total:
            from engrix_router.subscribers import usage as usage_mod

            folded: dict[str, int] = {}
            for frame in frames:
                found = usage_mod.extract(frame)
                if found:
                    folded = usage_mod.merge(folded, found)
            assert folded.get("total") == self.expected_usage_total, folded

    async def test_streaming_error_inside_the_stream_is_not_silently_empty(self, provider, creds,
                                                                            no_network):
        """An error frame must raise, not look like a model that went quiet (ADR-0001)."""
        self._serve(no_network, body=f'data: {json.dumps({"error": {"message": "boom"}})}\n\n')
        request = provider.build_request({"model": self.model, "stream": True, "messages": []},
                                         creds, stream=True)
        with pytest.raises(UpstreamError):
            async for _ in provider.open_stream(request):
                pass

    # ── 3. non-stream balikin chat.completion ──────────────────────────────
    async def test_complete_returns_canonical_completion(self, provider, creds, no_network):
        self._serve(no_network, body=self.chat_body, content_type=self.chat_content_type)
        request = provider.build_request({"model": self.model, "messages": []}, creds, stream=False)
        completion = await provider.complete(request)
        assert completion.get("object") == "chat.completion"
        message = completion["choices"][0]["message"]
        assert message["content"] == self.expected_content
        if self.expected_usage_total:
            from engrix_router.subscribers import usage as usage_mod

            assert usage_mod.canonicalize(completion.get("usage"))["total"] == self.expected_usage_total

    # ── 4. error HTTP keluar jadi UpstreamError bawa status vendor ────────
    async def test_error_status_becomes_an_upstream_error(self, provider, creds, no_network):
        self._serve(no_network, body=self.error_body, status=self.error_status,
                    content_type="application/json")
        request = provider.build_request({"model": self.model, "messages": []}, creds, stream=False)
        with pytest.raises(UpstreamError) as raised:
            await provider.complete(request)
        assert raised.value.status == self.error_status

    # ── 5. fragment tool call nyusun ulang per index ────────────────────────────
    async def test_tool_call_fragments_accumulate(self, provider, creds, no_network):
        """
        A split tool call must reassemble into valid JSON with a stable `index`.

        The bytes are provider-native on purpose (`tool_stream`): the invariant being
        tested is the CANONICAL frame shape, so each adapter proves it with its own wire
        format instead of with a stub that was written to look like OpenAI.
        """
        self._serve(no_network, body=self.tool_stream)
        request = provider.build_request({"model": self.model, "stream": True, "messages": []},
                                         creds, stream=True)
        collected = [frame async for frame in provider.open_stream(request)]
        assembled = openai_fmt.aggregate_stream_text(collected)["message"]["tool_calls"][0]
        assert json.loads(assembled["function"]["arguments"]) == self.expected_arguments
        assert assembled["function"]["name"] == self.expected_tool_name

    # ── 6. katalog + probe selalu punya default ───────────────────────────
    async def test_list_models_returns_specs(self, provider, creds, no_network):
        self._serve(no_network, body=self.models_body, content_type="application/json")
        models = await provider.list_models(creds)
        assert all(isinstance(model, ModelSpec) for model in models)

    async def test_probe_reports_a_tier_and_never_raises(self, provider, creds, no_network):
        self._serve(no_network, body=self.models_body, content_type="application/json")
        result = await provider.probe(creds)
        assert isinstance(result, ProbeResult)
        if self.probe_tier:
            assert result.tier == self.probe_tier

    # ── 7. hooks gak boleh nulis ulang content (ADR-0000) ──────────────────────────
    async def test_pipeline_does_not_mutate_the_client_body(self, provider, connection,
                                                            no_network):
        """
        ADR-0000 rule 4, gateway level: hooks and transforms may not rewrite the caller's body.

        Content equality alone would not catch a hook that edited `messages` in place;
        this compares the caller's dict before and after the whole run.
        """
        import copy

        from engrix_router.pipeline import runner

        self._serve(no_network, body=self.chat_body, content_type=self.chat_content_type)
        body = {"model": self.model, "messages": [{"role": "system", "content": "be brief"},
                                                 {"role": "user", "content": "hi"}],
                "temperature": 0.3}
        before = copy.deepcopy(body)
        await runner.run_chat(body)
        assert body == before, "the pipeline edited the caller's request body"

    async def test_gateway_path_delivers_upstream_content_unchanged(self, provider, connection,
                                                                    no_network, monkeypatch):
        """What the vendor said is what the client gets -- budgets/trace/etc. must not touch it."""
        from engrix_router.pipeline import runner

        self._serve(no_network, body=self.chat_body, content_type=self.chat_content_type)
        result = await runner.run_chat({"model": self.model,
                                        "messages": [{"role": "user", "content": "hi"}]})
        assert result["choices"][0]["message"]["content"] == self.expected_content
