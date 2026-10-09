"""Language policy enforcement (ADR-0000).

Placement decides the language:

* English  — identifiers, docstrings, log lines, client-visible error messages,
  UI strings, operator-facing settings documentation, all of ``docs/``.
* Indonesian — ``#`` comments inside code, and ``knowledge/`` (gitignored).

The AST never sees comments, which is why this file can be strict about strings
and docstrings without touching a single comment.

Debt is a REGISTRY, not a TODO. ``DEFERRED_*`` must match the current Indonesian
files exactly: converting one means deleting its line, and adding an Indonesian
docstring/string to a new file fails CI. The count can only shrink.
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "engrix_router"
# `scripts/` ikut scope: outputnya dibaca operator dan docstringnya menjelaskan cara
# jalan -- surface yang ke-publish, bukan catatan pribadi. `tests/` di luar scope:
# catatan di test adalah untuk kontributor, dan boleh Indonesia.
SHIPPED = (SRC, ROOT / "scripts")

# Kata yang jadi penanda teks Indonesia. Daftar sengaja sempit supaya teks English
# gak pernah kena false positive (loose tokens like "an"/"no" are NOT here).
INDONESIAN = re.compile(
    r"\b(yang|gak|nggak|buat|dipakai|digunakan|kalau|karena|harus|tidak|bisa|"
    r"sudah|udah|boleh|dengan|tanpa|supaya|biar|ini|itu|lo|gue|kita|saja|malah|"
    r"justru|ternyata|balikin|dibikin|dikasih|nyimpen|nimpa|ngeliat|jalanin|"
    r"dipake|sengaja|perlu|mesti|sekali|lagi|baru|lama|banyak|sedikit|belum|"
    r"sama|antara|setiap|diangkep|kebukti|kelar|sih|kok|dong|cuma|doang|juga|"
    r"bukan|ditolak|berbayar|butuh|kuota|katalog|umur|disalin|dikirim|diambil|"
    r"ketemu|gedhe|benerin|nulis|nyari|bikin|ngasih|pake|makai|sebagian|"
    r"saat|tadi|sekarang|sini|situ|jatuh|rusak|bocor|ketulis)\b",
    re.I,
)

# ---- DEFERRED DOCSTRINGS (remove lines as you convert; never add) -----------
# kosong = tuntas. Nambah entri di sini = ngelanggar ADR-0000, bukan nyicil kerjaan.
DEFERRED_DOCSTRINGS: set[str] = set()

# ---- DEFERRED PUBLIC DOCS (English single source; same rule) ----------------
DEFERRED_DOCS: set[str] = set()

# ---- DEFERRED UI TEMPLATES (strings must move to web/strings/{en,id}.json) --
# kosong = tuntas. Template-nya udah full catalog-driven + punya toggle locale,
# jadi gak ada lagi HTML yang nyimpen teks Indonesia di sini.
DEFERRED_UI: set[str] = set()


def _relative(path: pathlib.Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _docstrings(tree: ast.AST) -> list[str]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            text = ast.get_docstring(node, clean=False)
            if text:
                out.append(text)
    return out


def _string_literals(tree: ast.AST) -> list[str]:
    docs = set(_docstrings(tree))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and len(n.value) > 3 and n.value not in docs]


def _py_files(base: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in sorted(base.rglob("*.py")) if "__pycache__" not in str(p)]


def _shipped_files() -> list[pathlib.Path]:
    """Shipped surfaces: the package plus the tooling in scripts/ that operators run."""
    return [path for base in SHIPPED for path in _py_files(base)]


def test_operator_facing_strings_are_english():
    """Zero tolerance: log lines, error messages, settings docs, everything shipped."""
    offenders = []
    for path in _shipped_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for literal in _string_literals(tree):
            if INDONESIAN.search(literal):
                offenders.append(f"{_relative(path)}: {literal[:70]!r}")
    assert not offenders, (
        "string yang dibaca operator/klien harus English (ADR-0000), komentar # boleh "
        f"Indonesia:\n  " + "\n  ".join(offenders)
    )


def test_docstring_debt_registry_is_exact():
    still = {
        _relative(path) for path in _shipped_files()
        if any(INDONESIAN.search(text) for text in _docstrings(ast.parse(path.read_text(encoding="utf-8"))))
    }
    added = still - DEFERRED_DOCSTRINGS
    fixed = DEFERRED_DOCSTRINGS - still
    assert not added, f"docstring Indonesia baru di file yang belum terdaftar:\n  " + "\n  ".join(sorted(added))
    assert not fixed, (
        "file ini udah bersih, hapus dari DEFERRED_DOCSTRINGS (registry cuman boleh "
        f"mengecil):\n  " + "\n  ".join(sorted(fixed))
    )


def test_docs_and_adr_are_english():
    docs = [p for base in (ROOT / "docs",) for p in sorted(base.rglob("*.md"))] + [ROOT / "README.md"]
    still = {_relative(p) for p in docs if INDONESIAN.search(p.read_text(encoding="utf-8"))}
    added = still - DEFERRED_DOCS
    fixed = DEFERRED_DOCS - still
    assert not added, "dokumentasi publik harus English:\n  " + "\n  ".join(sorted(added))
    assert not fixed, f"hapus dari DEFERRED_DOCS, udah bersih:\n  " + "\n  ".join(sorted(fixed))


def test_ui_strings_live_in_the_string_catalog():
    """No template may hold operator-visible text: the catalogs do (ADR-0000).

    Done state: every label lives in ``web/strings/en.json`` + ``id.json`` (the en
    key set is the reference; id may add ``settings.doc.*`` overlays for backend
    docs, enforced by ``dashboard.py`` at import) and the dashboard carries a
    locale toggle. The regex scans the whole file, so JS string literals and
    comments count too -- an Indonesian word anywhere in an HTML template fails
    this. Registry empty.
    """
    html = [p for p in sorted(SRC.rglob("*.html"))]
    still = {_relative(p) for p in html if INDONESIAN.search(p.read_text(encoding="utf-8"))}
    assert still == DEFERRED_UI, (
        f"template dengan teks Indonesia harus = {sorted(DEFERRED_UI)}, sekarang {sorted(still)}; "
        "pindahkan labelnya ke web/strings/en.json + id.json"
    )
