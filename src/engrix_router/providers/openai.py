"""
OpenAI-compatible provider: the default shape for upstreams that speak
`/v1/chat/completions` + `/v1/models`.

Our version of 9router's DefaultExecutor (open-sse/executors/default.js:69-351):
almost no transformation, because the shapes already match. What we still keep:
  * the auth descriptor (bearer vs x-api-key) comes from TransportSpec, never
    hardcoded;
  * models come from the upstream list (not a static table), so new models are
    free;
  * probe = GET /models (cheap, no tokens) - see the testUtils.js:866 note in
    docs/ADDING_A_PROVIDER.md for why every provider must have a default probe.
"""
from __future__ import annotations

from engrix_router.core.types import ProviderDef, TransportSpec
from engrix_router.providers.base import BaseProvider

# Vendor generik dideklarasikan di module-nya sendiri, bukan lewat daftar di
# registry -- supaya 'tambah vendor' cukup nulis 1 file, gak nyentuh file kedua.
DEFINITIONS: tuple[ProviderDef, ...] = (
    ProviderDef(
        id="openai",
        category="apikey",
        display_name="OpenAI",
        aliases=("oai", "chatgpt"),
        auth_modes=("apikey",),
        transport=TransportSpec(
            base_url="https://api.openai.com/v1",
            models_url="https://api.openai.com/v1/models",
            default_model="gpt-4o-mini",
        ),
        probe_tier="models_list",
    ),
)


class OpenAICompatibleProvider(BaseProvider):
    def __init__(self, definition: ProviderDef) -> None:
        self.definition = definition


def build_node_definition(
    *,
    node_id: str,
    name: str,
    prefix: str,
    base_url: str,
    api_type: str = "chat",
    type_: str = "openai-compatible",
    auth_header: str = "Authorization",
    auth_prefix: str = "Bearer ",
) -> ProviderDef:
    """
    ProviderDef from a `nodes` table row (runtime, no code written).

    Counterpart of 9router's providerNodes plus its routing prefix
    (open-sse/services/model.js:44-61). Difference: the provider id we use is
    the prefix itself (`mycorp/gpt-4o`), not the node uuid, so logs and
    traces stay readable.

    """
    from engrix_router.core.types import AuthSpec, TransportSpec

    kind = "none" if not auth_header else ("api_key_header" if auth_header.lower() != "authorization" else "bearer")
    auth = AuthSpec(kind=kind, header=auth_header, prefix=auth_prefix if kind == "bearer" else "")
    chat_path = "/chat/completions" if api_type == "chat" else "/responses"
    transport = TransportSpec(
        base_url=base_url,
        chat_path=chat_path,
        models_path="/models",
        auth=auth,
        default_model=None,
    )
    return ProviderDef(
        id=prefix,
        category="apikey",
        display_name=name,
        aliases=(),
        auth_modes=("apikey",),
        transport=transport,
        features=frozenset({"node"} if type_ == "openai-compatible" else {"node", type_}),
        probe_tier="models_list",
    )
