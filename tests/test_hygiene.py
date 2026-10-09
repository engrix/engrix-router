"""Hygiene guards: dead imports and empty package files.

Clutter is the thing the owner hates most after bugs: an import that nothing uses
is a fake dependency edge - it makes a package look like it needs another, which
is exactly what the layer map (ADR-0003) is trying to say no to.
"""
from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "engrix_router"


def _modules():
    return [p for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in str(p)]


def _bound_and_used(tree: ast.AST) -> tuple[dict[str, int], set[str]]:
    imported: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                imported[alias.asname or alias.name.split(".")[0]] = node.lineno
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value
            if isinstance(base, ast.Name):
                used.add(base.id)
    return imported, used


def test_no_unused_imports():
    offenders = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported, used = _bound_and_used(tree)
        for name, line in imported.items():
            if name == "annotations":  # from __future__ import annotations
                continue
            if name not in used:
                offenders.append(f"{path.relative_to(SRC).as_posix()}:{line} -> {name}")
    assert not offenders, (
        "import yang gak dipakai (sisa refactor). Hapus, atau pake noqa plus "
        "alasannya kalau emang side-effect import:"
        + chr(10) + "  " + chr(10).join("  " + o for o in offenders)
    )


def test_every_package_has_an_init_and_a_docstring():
    """
    Every folder holding modules is a package with a reason to exist.

    The old version of this test walked ``__init__.py`` files, which meant a folder
    with NO ``__init__.py`` was invisible to it — it could not fail for something it
    never looked at. ``subscribers/`` slipped through exactly that way: it imported
    fine (setuptools' namespace discovery picked it up) while every other layer had
    a documented package file. Walk the folders instead.
    """
    offenders = []
    for path in sorted({p.parent for p in SRC.rglob("*.py") if "__pycache__" not in str(p)}):
        init = path / "__init__.py"
        rel = path.relative_to(SRC).as_posix() or "."
        if not init.exists():
            offenders.append(f"{rel}: tidak ada __init__.py")
            continue
        if not ast.get_docstring(ast.parse(init.read_text(encoding="utf-8"))):
            offenders.append(f"{rel}/__init__.py: tidak ada docstring")
    assert not offenders, (
        "folder layer harus jadi package yang ada alasannya:" + chr(10)
        + "  " + chr(10).join(offenders)
    )
