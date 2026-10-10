"""Renders the dashboard page (React SPA built by Vite).

The page itself is static/ui/index.html -- the product of ``npm run build`` in
web/ui. This module only injects the locale catalogs: operator-visible labels
are data, not markup (ADR-0000), they live in web/strings/{en,id}.json, load
once here, and reach the browser as one JSON blob (window.__ENGRIX__). The
default locale is the reference: every key it has, every other locale must
have too -- a label the browser cannot find is the failure mode this guards.

Why React now (TASK-43): the operator UI is realtime (quota countdowns, OAuth
polling, log streaming) and card-structured; a per-view innerHTML string painter
could not keep that honest. The build output is committed-free (static/ui is
gitignored); installing the package ships whatever was built at packaging time,
and verify_slice checks the built page is present.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.responses import HTMLResponse

from engrix_router.core import config

WEB_DIR = Path(__file__).resolve().parent
UI_DIST = WEB_DIR / "static" / "ui" / "index.html"
STRINGS_DIR = WEB_DIR / "strings"

LOCALES = ("en", "id")
DEFAULT_LOCALE = "en"


def _load_strings() -> dict[str, dict[str, str]]:
    """Read every locale catalog and refuse to serve a page with a hole in it."""
    catalogs: dict[str, dict[str, str]] = {}
    for code in LOCALES:
        path = STRINGS_DIR / f"{code}.json"
        catalogs[code] = json.loads(path.read_text(encoding="utf-8"))
    reference = catalogs[DEFAULT_LOCALE]
    problems = []
    for code, catalog in catalogs.items():
        if code == DEFAULT_LOCALE:
            continue
        missing = sorted(set(reference) - set(catalog))
        if missing:
            problems.append(f"{code} lacks {missing}")
    if problems:
        raise RuntimeError(
            f"locale catalog out of sync with the {DEFAULT_LOCALE} key set "
            f"(en has {len(reference)} keys): " + "; ".join(problems)
        )
    return catalogs


_STRINGS = _load_strings()


def render() -> HTMLResponse:
    if not UI_DIST.exists():
        return HTMLResponse(
            "<h1>dashboard not built</h1><p>run <code>npm run build</code> in "
            "src/engrix_router/web/ui, then reload.</p>",
            status_code=503,
        )
    # ensure_ascii=False bikin teks Indonesia tetep kebaca; "</" di-escape biar
    # label gak bisa nutup script tag tempat dia disisipin.
    strings = json.dumps(_STRINGS, ensure_ascii=False).replace("</", "<\\/")
    html = UI_DIST.read_text(encoding="utf-8")
    html = html.replace("{{STRINGS_JSON}}", strings)
    html = html.replace("{{DEFAULT_LOCALE}}", DEFAULT_LOCALE)
    html = html.replace("{{APP_VERSION}}", config.VERSION)
    html = html.replace("{{APP_NAME}}", config.APP_NAME)
    html = html.replace("{{CLIENT_BASE}}", f"http://{config.HOST}:{config.PORT}")
    # no-store di shell HTML: katalog disuntik KE dokumen ini, jadi salinan
    # cache = UI basi (browser sempat nampilkan label mentah sehabis restart
    # server -- TASK-44). JS/CSS di sampingnya sudah di-hash isinya oleh
    # Vite, jadi cache selamanya buat file itu tetap aman.
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})
