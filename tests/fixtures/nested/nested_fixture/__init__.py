"""Provider yang berbentuk FOLDER (paket), fixture tests/test_registry_discovery.py.

Bentuknya begini begitu satu vendor butuh lebih dari satu file -- fixture, helper
envelope, dsb. Discovery wajib sama-sama bisa: modul datar (`qoder.py`) dan paket
(`codebuddy/`). Ini yang bikin satu distribusi provider privat bisa nampung 30 vendor
tanpa bikin satu repo per vendor.
"""
from engrix_router.core.types import ProviderDef, TransportSpec

from ._detail import PRICE_FACTOR

DEFINITIONS = (
    ProviderDef(
        id="nested-fixture",
        category="apikey",
        display_name=f"Nested fixture ({PRICE_FACTOR})",
        transport=TransportSpec(base_url="https://nested.invalid/v1"),
    ),
)
