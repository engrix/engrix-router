"""
Provider registry: built-ins (this package) + providers shipped elsewhere.

Three sources, one contract, scanned instead of hand-listed:

1. built-in modules and packages under ``engrix_router/providers/``;
2. installed distributions publishing an entry point in the group
   ``engrix_router.providers`` (ADR-0002: how a private adapter attaches without
   editing this repo);
3. ``EROUTER_PROVIDERS_PATH`` (directories holding provider modules) -- a
   development override so a private adapter can run from a checkout without
   building a wheel.

The contract is the same for every source, and deliberately small:

    DEFINITIONS: tuple[ProviderDef, ...]        # required to be a vendor
    PROVIDER_CLASS: type[BaseProvider]          # optional, default OpenAICompatibleProvider

Why this shape, from concrete findings in 9router v0.5.95:

1. The registry is scanned, not a hand-maintained import list.
   9router comments providers/registry/index.js as "auto-generated" while it is
   written by hand and no generator exists (AGENTS.md:37 claims one). A missed
   import = a provider silently missing from the UI and from parseModel.
   Scanning is what makes "add a vendor = write one file" actually true.
2. An unknown provider is a hard error, never a fallback.
   In 9router getExecutor falls through to DefaultExecutor with
   `PROVIDERS[provider] || PROVIDERS.openai` (executors/default.js:71) and
   getTargetFormat returns "openai" (services/provider.js:130-142) - so a
   mistyped prefix quietly shoots at api.openai.com with whatever key is loaded.
3. A provider that fails to load is isolated per provider and LOUD. One broken
   adapter must not take the working ones down, and must not vanish silently
   either: the error is recorded and served by /health and /api/state.
4. One prefix from two sources is a startup error, not "first one wins" -
   a shadowed prefix routes to a vendor the operator never picked.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from engrix_router.core import config
from engrix_router.storage.sqlite import query, rows_to_dicts
from engrix_router.core.types import ProviderDef
from engrix_router.providers.openai import OpenAICompatibleProvider, build_node_definition

ENTRY_POINT_GROUP = "engrix_router.providers"

# ── penemuan provider: SATU TEMPAT, tanpa daftar manual ──────────────────────
# Kontraknya simple dan sama untuk semua sumber (module built-in, entry point,
# folder EROUTER_PROVIDERS_PATH):
#   DEFINITIONS: tuple[ProviderDef, ...]     (wajib kalo nyediain vendor)
#   PROVIDER_CLASS: type[BaseProvider]       (opsional; default OpenAICompatibleProvider)
# Kenapa scan, bukan daftar import: lihat angka 1 di docstring modul.
_PACKAGE_DIR = Path(__file__).resolve().parent
# Nama paket buat import_module diambil dari __name__, bukan literal "src.providers".
# Kemarin literal dan langsung mati pas paket di-rename -- yang nelpon scan ini gak
# boleh tau nama paketnya.
_PACKAGE = __name__.rpartition(".")[0]
_SKIP_MODULES = {"__init__", "base", "registry"}

_lock = threading.RLock()
_instances: dict[str, Any] = {}
_by_prefix: dict[str, ProviderDef] = {}
_class_by_id: dict[str, type] = {}
_load_errors: dict[str, str] = {}
_prefix_owner: dict[str, str] = {}
# Modul yang declare tiap prefix. Dua jalur discovery (entry point + EROUTER_PROVIDERS_PATH)
# bisa nemuin FILE yang sama saat paket di-install `pip install -e` DAN dev override aktif
# sekaligus -- itu bukan tabrakan, itu modul yang sama. Yang dilarang: dua modul BERBEDA
# mengklaim prefix sama (routing jadi lempar koin).
_prefix_module: dict[str, Any] = {}
_node_cache: dict[str, dict[str, Any]] | None = None
_discovered = False


def _discover(*, force: bool = False) -> dict[str, ProviderDef]:
    global _discovered
    with _lock:
        if _discovered and not force:
            return dict(_by_prefix)
        _by_prefix.clear()
        _class_by_id.clear()
        _prefix_owner.clear()
        _prefix_module.clear()
        _load_errors.clear()
        for source, name, load in _sources():
            label = f"{source}:{name}"
            try:
                _register(load(), label)
            except DuplicatePrefix:
                raise                      # tabrakan prefix = mati di startup, jangan dicatat doang
            except Exception as exc:
                # Adapter rusak gak boleh nimpa adapter lain, dan gak boleh senyap.
                _load_errors[label] = f"{exc.__class__.__name__}: {exc}"[:400]
        _discovered = True
        return dict(_by_prefix)


def _builtin_sources() -> Iterator[tuple[str, str, Callable[[], Any]]]:
    for path in sorted(_PACKAGE_DIR.glob("*.py")):
        name = path.stem
        if name in _SKIP_MODULES or name.startswith("_"):
            continue
        module_name = f"{_PACKAGE}.{name}"
        yield ("builtin", name, lambda module_name=module_name: importlib.import_module(module_name))
    for directory in sorted(p for p in _PACKAGE_DIR.iterdir() if p.is_dir()):
        name = directory.name
        if name.startswith(("_", ".")) or not (directory / "__init__.py").exists():
            continue
        module_name = f"{_PACKAGE}.{name}"
        yield ("builtin", name, lambda module_name=module_name: importlib.import_module(module_name))


def _register(module: Any, label: str) -> None:
    definitions = getattr(module, "DEFINITIONS", ())
    if not definitions:
        return
    klass = getattr(module, "PROVIDER_CLASS", None) or OpenAICompatibleProvider
    for definition in definitions:
        for prefix in definition.prefixes:
            holder = _prefix_owner.get(prefix)
            if holder is not None and holder != label:
                if _prefix_module.get(prefix) is module:
                    # Satu modul yang sama ketemu lewat dua jalur discovery (entry point
                    # + EROUTER_PROVIDERS_PATH saat paket ter-install editable): satu
                    # objek, satu kelas -- bukan koin, label pertama tetap punya prefix.
                    continue
                raise DuplicatePrefix(
                    f"provider prefix '{prefix}' is declared twice: {holder} and {label}"
                )
            _prefix_owner[prefix] = label
            _prefix_module[prefix] = module
            _by_prefix[prefix] = definition
            _class_by_id[definition.id] = klass


def _dotted_name(path: Path) -> str | None:
    """Dotted import name for a file living under one of sys.path entries, if any."""
    for entry in sys.path:
        if not entry:
            continue
        try:
            base = Path(entry).resolve()
            relative = path.resolve().relative_to(base)
        except (OSError, ValueError):
            continue
        parts = list(relative.with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else None
    return None


def _import_from_path(path: Path, fallback_name: str) -> Any:
    """
    Import a path-provided module under its REAL name when sys.path allows it.

    Loading the same file twice under two names would give two provider classes, and
    a test or script that patches/imports one would silently disagree with the one the
    registry routes to. Real name first, file load only as a fallback.
    """
    dotted = _dotted_name(path)
    if dotted:
        try:
            return importlib.import_module(dotted)
        except ImportError:
            pass
    return _load_file(path, fallback_name)


def _path_sources() -> Iterator[tuple[str, str, Callable[[], Any]]]:
    """EROUTER_PROVIDERS_PATH: dev override so an external adapter runs from a checkout, no wheel."""
    for index, directory in enumerate(config.PROVIDER_PATHS):
        if not directory.is_dir():
            _load_errors[f"EROUTER_PROVIDERS_PATH:{directory}"] = "directory does not exist"
            continue
        # Nama modul dibuat unik: file luar boleh senama dengan built-in tanpa
        # saling nimpa di sys.modules.
        for path in sorted(directory.glob("*.py")):
            name = path.stem
            if name in _SKIP_MODULES or name.startswith("_"):
                continue
            module_name = f"_engrix_external_provider_{index}_{name}"
            yield ("providers-path", name,
                   lambda path=path, module_name=module_name: _import_from_path(path, module_name))
        for package in sorted(p for p in directory.iterdir() if p.is_dir()):
            init = package / "__init__.py"
            if package.name.startswith(("_", ".")) or not init.exists():
                continue
            module_name = f"_engrix_external_provider_{index}_{package.name}"
            yield ("providers-path", package.name,
                   lambda init=init, module_name=module_name: _import_from_path(init, module_name))


def _sources() -> Iterator[tuple[str, str, Callable[[], Any]]]:
    yield from _builtin_sources()
    for entry in _entry_points():
        yield ("entry-point", entry.name, entry.load)
    yield from _path_sources()


def _load_file(path: Path, module_name: str) -> Any:
    # Kalau yang di-load `__init__.py` sebuah folder, folder itu harus ikut jadi
    # search path -- kalau gak, submodulnya (vendor yang jadi paket) gak ke-resolve.
    search = [str(path.parent)] if path.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(module_name, str(path),
                                                  submodule_search_locations=search)
    if spec is None or spec.loader is None:
        raise ImportError(f"no import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    # Harus masuk sys.modules SEBELUM exec: paket butuh itu buat resolve submodulnya.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _entry_points() -> list[Any]:
    from importlib.metadata import entry_points

    found = entry_points()
    if hasattr(found, "select"):
        return sorted(found.select(group=ENTRY_POINT_GROUP), key=lambda e: e.name)
    return sorted(found.get(ENTRY_POINT_GROUP, []), key=lambda e: e.name)


def load_errors() -> dict[str, str]:
    """Provider sources that failed to load, by label. Empty means every source imported."""
    _discover()
    return dict(_load_errors)


def reload_providers() -> int:
    """Drop cached instances and rescan every source. Returns how many prefixes were found."""
    _instances.clear()
    return len(_discover(force=True))


def builtin_definitions() -> list[ProviderDef]:
    _discover()
    seen: dict[str, ProviderDef] = {}
    for definition in _by_prefix.values():
        seen[definition.id] = definition
    return [seen[key] for key in sorted(seen)]


def node_prefixes() -> dict[str, dict[str, Any]]:
    """Runtime providers from the `nodes` table, cached per process (invalidated on node writes)."""
    global _node_cache
    with _lock:
        if _node_cache is None:
            _node_cache = {row["prefix"]: row for row in rows_to_dicts(query(
                "SELECT id, name, prefix, type, api_type, base_url, is_active FROM nodes"
            ))}
        return dict(_node_cache)


def invalidate_nodes() -> None:
    global _node_cache
    with _lock:
        _node_cache = None
        builtin_ids = {d.id for d in builtin_definitions()}
        for key in [k for k in _instances if k not in builtin_ids]:
            _instances.pop(key, None)


def definitions(include_nodes: bool = True) -> list[ProviderDef]:
    """Every provider the gateway can route to: code-declared first, then runtime nodes."""
    out = builtin_definitions()
    if include_nodes:
        for row in node_prefixes().values():
            if not row.get("is_active", 1):
                continue
            out.append(_definition_from_node_row(row))
    return out


def _definition_from_node_row(row: dict[str, Any]) -> ProviderDef:
    return build_node_definition(
        node_id=row["id"],
        name=row["name"],
        prefix=row["prefix"],
        base_url=row["base_url"],
        api_type=row.get("api_type") or "chat",
        type_=row.get("type") or "openai-compatible",
    )


def get_definition(prefix: str) -> ProviderDef | None:
    definition = _discover().get(prefix)
    if definition is not None:
        return definition
    row = node_prefixes().get(prefix)
    if row is None or not row.get("is_active", 1):
        return None
    return _definition_from_node_row(row)


def get_provider(prefix: str):
    """
    One instance per provider id, built from the class that module declares.

    This is the dispatch point. The engine never asks "is this a special
    vendor or a generic one" - it just calls get_provider(), so adding a
    vendor with its own protocol never touches the engine.

    """
    _discover()
    definition = get_definition(prefix)
    if definition is None:
        raise UnknownProvider(f"provider '{prefix}' is not registered")
    with _lock:
        instance = _instances.get(definition.id)
        if instance is None:
            klass = _class_by_id.get(definition.id, OpenAICompatibleProvider)
            instance = klass(definition)
            _instances[definition.id] = instance
        return instance


class UnknownProvider(Exception):
    pass


class DuplicatePrefix(RuntimeError):
    """Two sources claim the same model prefix -- routing would be a coin flip."""


class UnknownModel(Exception):
    pass


def split_model(raw: str) -> tuple[str, str]:
    """
    `prefix/model` -> (prefix, model). A model without a prefix is a hard error.

    9router happily returns the "openai" fallback for a bare token
    (services/model.js:126-155). We do not: this gateway is used by engrix,
    which writes explicit model ids in its yaml, and guessing a provider from
    an empty string means silently sending to the wrong vendor.

    """
    text = (raw or "").strip()
    if "/" not in text:
        raise UnknownModel(
            f"model '{raw}' must be provider/model (e.g. openai/gpt-4o-mini)"
        )
    prefix, model = text.split("/", 1)
    if not prefix or not model:
        raise UnknownModel(f"model '{raw}' is invalid")
    return prefix, model


def resolve(raw: str) -> tuple[ProviderDef, str]:
    prefix, model = split_model(raw)
    definition = get_definition(prefix)
    if definition is None:
        raise UnknownProvider(f"provider '{prefix}' is not registered (node/built-in)")
    return definition, model
