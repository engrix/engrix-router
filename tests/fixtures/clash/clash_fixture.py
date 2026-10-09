"""A provider that claims a prefix the built-ins already own -- discovery fixture.

Shadowing is the silent-routing bug this registry refuses to have: if two sources
declare `openai`, whichever imported first would decide where traffic goes. The
registry raises DuplicatePrefix instead (see tests/test_registry_discovery.py).
"""
from engrix_router.core.types import ProviderDef, TransportSpec

DEFINITIONS = (
    ProviderDef(
        id="openai",
        category="apikey",
        display_name="Clashing fixture (test only)",
        transport=TransportSpec(base_url="https://fixture.invalid/v1"),
    ),
)
