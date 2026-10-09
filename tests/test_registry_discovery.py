"""Guard: provider discovery from outside the package works, and fails loudly.

ADR-0002 makes external providers the normal case (subscription adapters live in a
private distribution), so the mechanism gets its own tests instead of trust:
a filesystem path, an installed entry point, one broken adapter that must not take
the others down, and a duplicate prefix that must be an error rather than a shadow.

Fixtures live in tests/fixtures/{good,broken,clash}/ as real files -- pytest does not
collect them (no test_ prefix), the registry does.
"""
from __future__ import annotations

import pathlib
import types

import pytest

from engrix_router.core import config
from engrix_router.core.types import ProviderDef, TransportSpec
from engrix_router.providers import registry

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def rescan():
    """Reload the registry for the test, and put the real state back afterwards."""
    registry.reload_providers()
    yield
    config.PROVIDER_PATHS = []
    registry.reload_providers()


def test_providers_path_registers_an_external_provider(rescan, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_PATHS", [FIXTURES / "good"])
    registry.reload_providers()
    definition = registry.get_definition("discovery-fixture")
    assert definition is not None and definition.id == "discovery-fixture"
    assert registry.get_provider("discovery-fixture").definition is definition
    assert registry.load_errors() == {}


def test_a_broken_adapter_is_loud_but_does_not_take_others_down(rescan, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_PATHS", [FIXTURES / "broken", FIXTURES / "good"])
    registry.reload_providers()
    errors = registry.load_errors()
    assert [label for label in errors if "broken" in label], errors
    assert "RuntimeError" in errors[list(errors)[0]]
    # yang rusak cuma satu: sisanya (built-in dan fixture yang sehat) tetap ke-route.
    assert registry.get_definition("openai") is not None
    assert registry.get_definition("discovery-fixture") is not None


def test_duplicate_prefix_is_a_hard_error(rescan, monkeypatch):
    monkeypatch.setattr(config, "PROVIDER_PATHS", [FIXTURES / "clash"])
    with pytest.raises(registry.DuplicatePrefix, match="openai"):
        registry.reload_providers()


def test_a_provider_can_be_a_FOLDER_not_only_a_file(rescan, monkeypatch):
    """Vendor yang tumbuh jadi paket (fixture + helper) harus tetep ke-discover.

    Ini jalur yang dipakai distribusi provider privat begitu satu adapter gak muat
    di satu file -- dan paketnya ngimport submodul sendiri, jadi search path-nya
    harus ikut ke-setel.
    """
    monkeypatch.setattr(config, "PROVIDER_PATHS", [FIXTURES / "nested"])
    registry.reload_providers()
    definition = registry.get_definition("nested-fixture")
    assert definition is not None
    assert definition.display_name == "Nested fixture (0.1)"   # submodulnya kebaca
    assert registry.load_errors() == {}


def test_entry_points_are_scanned(rescan, monkeypatch):
    """Entry-point discovery is tested by faking the distribution, not by installing one."""

    class FakeEntryPoint:
        name = "fixture"

        def load(self):
            module = types.ModuleType("fixture_via_entry_point")
            module.DEFINITIONS = (ProviderDef(
                id="entrypoint-fixture", category="apikey",
                transport=TransportSpec(base_url="https://fixture.invalid/v1")),)
            return module

    monkeypatch.setattr(registry, "_entry_points", lambda: [FakeEntryPoint()])
    registry.reload_providers()
    assert registry.get_definition("entrypoint-fixture") is not None
    assert registry.load_errors() == {}
