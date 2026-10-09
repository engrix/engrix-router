"""Guard: reverse-engineered vendor material never enters this public repository.

ADR-0002 splits the project in two because shipping a vendor's RSA key, its
obfuscation alphabet, its undocumented paths and its impersonated client-identity
headers IS publishing that material -- plus it hands the vendor a one-file diff to
break us. Those artifacts live in ONE private distribution, `engrix-router-providers`,
and this test is what keeps the boundary from rotting: deleting the file is easy,
keeping it out is the part that needs enforcement.

Markers are assembled from fragments on purpose: written whole, this file would
match its own rule. `knowledge/` and `data/` are skipped -- they are gitignored and
allowed to name sandbox paths.
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

PRIVATE_MARKERS = [
    "-----BEGIN " + "PUBLIC KEY-----",
    "_do" + "RTgHZBKcGVjlvpC",              # custom base64 alphabet
    "/algo" + "/api/v2/",                     # undocumented endpoint paths
    "cosy-" + "machineid",                    # impersonated client-identity headers
    "agent_chat" + "_generation",
    "ENGRIX-AGENT" + "-SANDBOX",             # the private RE sandbox
]

SKIP_DIR_PARTS = {"__pycache__", "data", "knowledge", ".pytest_cache", "build", ".venv", ".git"}
SCAN_EXT = {".py", ".md", ".sql", ".html", ".toml", ".txt", ".cfg", ".yaml", ".yml", ".json", ".example"}

# Yang WAJIB di-ignore: dapur, DB berisi prompt nyata, dan kredensial.
MUST_IGNORE = ["knowledge/", "data/", "credentials/", ".env"]


PRIVATE_MODULE = "engrix_router_" + "providers"   # fragile lagi, lihat catatan di atas


def _files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_EXT:
            continue
        if SKIP_DIR_PARTS & set(path.parts):
            continue
        yield path


def test_no_private_provider_material():
    offenders = []
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in PRIVATE_MARKERS:
            if marker in text:
                rel = path.relative_to(ROOT).as_posix()
                line = text[: text.index(marker)].count("\n") + 1
                offenders.append(f"{rel}:{line} contains vendor material")
    assert not offenders, (
        "materi hasil RE gak boleh ada di repo publik (ADR-0002) -- pindahin ke paket "
        "adapter privat:\n  " + "\n  ".join(offenders)
    )


def test_the_kitchen_stays_ignored():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    missing = [entry for entry in MUST_IGNORE if entry not in gitignore]
    assert not missing, f".gitignore gak nge-ignore {missing}: DB, .env, dan knowledge/ wajib luar"


def test_public_core_does_not_import_the_private_adapter():
    """The core may name a provider id in prose, but never imports the private package."""
    offenders = []
    for path in _files():
        if path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if PRIVATE_MODULE in text:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders, f"core publik mengimpor adapter privat: {offenders}"
