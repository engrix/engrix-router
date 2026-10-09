"""Guard: no stale module paths or local disk layout after the renames (ADR-0003).

Every rename has to leave zero references behind, otherwise the next reader follows a
path that no longer exists. References that are *about* history (the old -> new table in
ADR-0003, the comment in registry.py naming the literal it replaced) are allowed per
exact file, never per pattern, so a NEW stale reference still fails.

`data/` and `knowledge/` are skipped: the first is runtime output, the second is the
gitignored kitchen drawer that is allowed - and expected - to name sandbox paths.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
NEWLINE = chr(10)

STALE = [
    r"\bsrc\.(api|router|providers|web|config|db|ids|shared)\b",
    r"\bsrc/(api|router|providers|web|schema\.sql|config\.py|db\.py|ids\.py)\b",
    r"\bengrix_router\.router\b",
    r"\bengrix_router\.db\b",
    r"\bengrix_router\.config\b",
    r"\bengrix_router\.ids\b",
    r"\bclient_keys\b",
    r"\bkey_store\b",
    r"\bcontracts\.py\b",
    r"\bgw/|EGW_|engrix-graph-api|graph_api\b",
    r"providers/http_client",
    r"router/(engine|routing|quota|connections|health|budget|trace|usage|pricing|logger|settings|sse|proxy|url_guard|client_keys)\.py",
    # ADR-0004: nama modul format/route/provider sudah disimetrikan. Nama lama = path yang gak
    # ada lagi, jadi rujukan ke dia harus gagal di CI, bukan ngarahin pembaca ke file hantu.
    r"\bopenai_sse\b",
    r"\banthropic_messages\b",
    r"\bopenai_compatible\b",
    r"\bapi[/.]compat\b",
]

# File yang boleh nyebut nama lama karena memang lagi ngebahas sejarah.
ALLOWED = {
    "CHANGELOG.md",                     # release notes: nama yang dipensiun itu isi catatannya
    "docs/adr/0003-layer-map.md",
    "docs/adr/0004-file-naming.md",
    "src/engrix_router/providers/registry.py",
    "tests/test_no_stale_references.py",
}
SKIP_DIR_PARTS = {"__pycache__", "data", "knowledge", ".pytest_cache", "build", ".venv", ".git"}
TEXT_EXT = {".py", ".md", ".sql", ".html", ".toml", ".txt", ".cfg", ".yaml", ".yml"}
PATH_EXT = {".py", ".md", ".css", ".html", ".toml", ".sql", ".yaml", ".yml"}
# drive-letter + potongan path lokal owner (working dir, home, sandbox) - contoh
# teksnya sengaja gak ditulis di sini, kalau ditulis guard ini nangkap dirinya sendiri.
# Karakter backslash di dalam character class regex butuh dua biji, makanya chr(92)*2.
ABSOLUTE = re.compile("[A-Za-z]:[/" + chr(92) * 2 + "](?:engrix|dev|Users|Documents|ENGRIX)")


def _files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in TEXT_EXT:
            continue
        parts = set(path.parts)
        if SKIP_DIR_PARTS & parts:
            continue
        # artefak build (src/*.egg-info, build/, dist/) nyimpen daftar file versi lama;
        # itu histori build, bukan rujukan yang bisa diperbaiki.
        if any(part.endswith(".egg-info") or part in {"dist"} for part in parts):
            continue
        yield path


def test_no_stale_references():
    offenders = []
    for path in _files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in STALE:
            for match in re.finditer(pattern, text):
                line = text[: match.start()].count(NEWLINE) + 1
                offenders.append(f"{rel}:{line} -> {match.group(0)!r}")
    assert not offenders, (
        "referensi path/nama lama sisa rename. Benerin, atau masukin file ke ALLOWED "
        "dengan alasan di ADR (harus catatan sejarah, bukan pointer rusak):"
        + NEWLINE + "  " + NEWLINE.join(offenders)
    )


def test_no_local_absolute_paths():
    """A public repo must not carry the owner's local disk layout.

    Found during the audit: several files still pointed at drive-letter paths to the
    consumer, the brand pack and the RE sandbox - that leaks private structure and
    makes the documentation un-followable for anyone else.
    """
    offenders = []
    for path in _files():
        if path.suffix not in PATH_EXT:
            continue
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in ABSOLUTE.finditer(text):
            line = text[: match.start()].count(NEWLINE) + 1
            offenders.append(f"{rel}:{line} -> {match.group(0)!r}")
    assert not offenders, (
        "path absolut lokal di surface publik:" + NEWLINE + "  " + NEWLINE.join(offenders)
    )


def test_allowed_history_stays_small():
    """ALLOWED gak boleh nambah diam-diam; tiap entri utang satu alasan di ADR/changelog."""
    assert len(ALLOWED) <= 5, "ALLOWED nambah? kasih alasan di docs/adr, bukan di test"
