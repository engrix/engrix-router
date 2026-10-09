"""
Copy this file to add a vendor: `cp _template.py myvendor.py` -- that is the whole wiring.

The registry scans this folder and never a hand-maintained import list (ADR-0002), so
a new provider is one file and nothing else:

  * the module declares ``DEFINITIONS`` (what the vendor is: ids, base URL, auth style)
    and optionally ``PROVIDER_CLASS`` (how it behaves);
  * the name of this file is skipped because it starts with ``_``, so the template can
    carry real code without becoming a routable provider;
  * ``if provider == "myvendor"`` outside this file is a review rejection (ADR-0000):
    if a decision belongs to the vendor, it lives with the vendor.

A vendor that only differs by URL and key header needs no class at all --
``OpenAICompatibleProvider`` is the default and the runtime ``nodes`` table already
covers that case without writing code. Write a class when the wire protocol differs:
then override the smallest hook in ``providers/base.py`` that fits
(``transform_request`` payload, ``encode_body`` bytes, ``sign_request`` signature,
``unwrap_data`` frame), and never store per-request state on ``self`` -- one instance
is shared by every concurrent request and every account.

Then run the contract suite against it (``engrix_router.testing.contract``) with canned
upstream bodies: it proves canonical-shape output, tool-call reassembly, error mapping
and the no-content-mutation invariant without network. ``docs/ADDING_A_PROVIDER.md`` is
the long version of these instructions.
"""
from __future__ import annotations

from engrix_router.core.types import (
    AUTH_API_KEY_HEADER,
    AuthSpec,
    Credentials,
    ProviderDef,
    TransportSpec,
)
from engrix_router.providers.base import BaseProvider


class TemplateProvider(BaseProvider):
    """Override only what this vendor actually does differently -- start with none."""

    def transform_request(self, body: dict, *, creds: Credentials, stream: bool) -> dict:
        """
        Default is fine for a plain OpenAI-shaped vendor; delete this method if so.

        It receives `creds` instead of storing it on `self`, because the instance is a
        singleton: state here would be shared between two accounts mid-request.
        """
        return super().transform_request(body, creds=creds, stream=stream)


DEFINITIONS: tuple[ProviderDef, ...] = (
    ProviderDef(
        id="myvendor",                        # prefix buat model: myvendor/model-x
        category="apikey",                    # pilihannya: apikey | oauth | none
        display_name="My Vendor",
        aliases=(),                           # prefix tambahan, contohnya ("mv",)
        auth_modes=("apikey",),
        features=frozenset(),                 # {"usage"} kalau fetch_quota() ikut dibuat
        transport=TransportSpec(
            base_url="https://api.example.invalid/v1",
            chat_path="/chat/completions",
            models_path="/models",
            auth=AuthSpec(kind=AUTH_API_KEY_HEADER, header="x-api-key", prefix=""),
            default_model="model-x",
        ),
        probe_tier="models_list",             # pilihannya: models_list | one_token_chat | key_exchange
    ),
)

PROVIDER_CLASS = TemplateProvider
