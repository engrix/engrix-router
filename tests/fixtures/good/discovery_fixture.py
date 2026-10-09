"""A provider shipped outside the package -- fixture for tests/test_registry_discovery.py.

Nothing here runs in production: the prefix is not a vendor, the base URL is invalid,
and the module exists only to prove the registry scans EROUTER_PROVIDERS_PATH and entry
points the same way it scans its own folder.
"""
from engrix_router.core.types import ProviderDef, TransportSpec

DEFINITIONS = (
    ProviderDef(
        id="discovery-fixture",
        category="apikey",
        display_name="Discovery fixture (test only)",
        transport=TransportSpec(base_url="https://fixture.invalid/v1"),
    ),
)
