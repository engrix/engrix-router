"""Layer rules enforced in CI — no external dependency.

The architecture contract (ADR-0000 rule "layer, top to bottom"):

    core < storage < transport = identity = auth < providers = accounts
         < routing = hooks = subscribers = services < pipeline < api < web

Two invariants are checked here:

1. Direction — a module may only import from its own layer or below it.
2. Independence — siblings in the same band must not import each other:
   ``{routing, hooks, subscribers, services}`` and ``{providers, accounts}``.
   One decision, one owner: ``accounts`` lists candidate connections and never
   resolves a provider (that is the pipeline's job through ``providers.registry``).

``import-linter`` is the belt-and-braces version of this file for a real install
(``[dev]`` extra); this test exists so the rule is enforced even where that tool is
not installed — which is exactly the situation a fresh clone is in.

Keep the language policy honest: English docstring, Indonesian comments below are
allowed (ADR-0000).
"""
from __future__ import annotations

import ast
import pathlib

PKG_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "engrix_router"

RANK = {
    "core": 0,
    "storage": 1,
    "transport": 2,
    "identity": 2,
    "auth": 2,
    "providers": 3,
    "accounts": 3,
    "routing": 4,
    "hooks": 4,
    "subscribers": 4,
    "services": 4,
    "pipeline": 5,
    "api": 6,
    # `testing` = suite kontrak yang di-export buat paket provider (ADR-0002).
    # Dia boleh nyentuh pipeline (tes invarian lewat jalur penuh), jadi rangknya
    # bareng api; yang bawah gak boleh nyembul ke atas.
    "testing": 6,
    "web": 7,
}
SIBLING_GROUPS = (
    frozenset({"routing", "hooks", "subscribers", "services"}),
    frozenset({"providers", "accounts"}),
)
# Modul di root paket (__init__.py) gak punya layer; dia cuma bootstrap.
ROOT_MODULES = "root"


def _source_layer(path: pathlib.Path) -> str:
    parts = path.relative_to(PKG_ROOT).parts
    return parts[0] if len(parts) > 1 else ROOT_MODULES


def _imported_layers(tree: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
        elif isinstance(node, ast.ImportFrom) and node.level and node.module:
            # from . import x / from ..y import z -- relatif, jadi dihitung dari file ini
            modules = [f"__relative__.{node.module}"]
        for module in modules:
            if not module.startswith("engrix_router"):
                continue
            pieces = module.split(".")
            if len(pieces) > 1 and pieces[1] in RANK:
                out.append((pieces[1], getattr(node, "lineno", 0)))
    return out


def _all_modules() -> list[pathlib.Path]:
    return [p for p in sorted(PKG_ROOT.rglob("*.py")) if "__pycache__" not in str(p)]


def test_package_tree_only_contains_known_layers():
    unknown = sorted({layer for p in _all_modules()
                      for layer in [_source_layer(p)]
                      if layer != ROOT_MODULES and layer not in RANK})
    assert not unknown, f"layer tidak terdaftar di RANK: {unknown}"


def test_no_upward_imports():
    bad: list[str] = []
    for path in _all_modules():
        source = _source_layer(path)
        if source == ROOT_MODULES:
            continue
        for target, line in _imported_layers(ast.parse(path.read_text(encoding="utf-8"))):
            if RANK[target] > RANK[source]:
                bad.append(f"{source} -> {target} pada {path.relative_to(PKG_ROOT)}:{line}")
    assert not bad, "layer bawah ngimpor layer atas:\n  " + "\n  ".join(bad)


def test_siblings_stay_independent():
    bad: list[str] = []
    for path in _all_modules():
        source = _source_layer(path)
        if source == ROOT_MODULES:
            continue
        for target, line in _imported_layers(ast.parse(path.read_text(encoding="utf-8"))):
            if target == source:
                continue
            same_group = any(source in group and target in group for group in SIBLING_GROUPS)
            if same_group:
                bad.append(f"{source} -> {target} pada {path.relative_to(PKG_ROOT)}:{line}")
    assert not bad, (
        "sesama satu band saling import — satu keputusan harus punya satu pemilik:\n  "
        + "\n  ".join(bad)
    )


def test_root_package_imports_nothing_upward():
    """`engrix_router/__init__.py` = bootstrap doang, bukan tempat ngumpul logic."""
    init = PKG_ROOT / "__init__.py"
    if not init.exists():
        return
    tree = ast.parse(init.read_text(encoding="utf-8"))
    assert not _imported_layers(tree), "root __init__ tidak boleh import layer mana pun"
