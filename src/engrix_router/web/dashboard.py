"""Renders the dashboard page (static HTML + vanilla JS, no build step).

Why not React/Vite like engrix-agent/web: this gateway is a single-process Python
service; putting a JS toolchain inside the repo = two build systems for one admin
page. What is taken from the engrix web app is its THEME (identical CSS tokens,
see web/static/engrix.css), not its stack.

Operator-visible labels are data, not markup (ADR-0000): they live in
web/strings/{en,id}.json, load once here, and travel to the browser as one JSON
blob that the template paints through a locale toggle. No template engine -- the
page is served by plain placeholder replacement, so there is no Jinja dependency.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.responses import HTMLResponse

from engrix_router.core import config

WEB_DIR = Path(__file__).resolve().parent
TEMPLATE = WEB_DIR / "templates" / "dashboard.html"
STRINGS_DIR = WEB_DIR / "strings"

LOCALES = ("en", "id")
DEFAULT_LOCALE = "en"


def _load_strings() -> dict[str, dict[str, str]]:
    """Read every locale catalog and refuse to serve a page with a hole in it.

    The default locale is the reference: every key it has, every other locale
    must have too -- a label the browser cannot find is the failure mode this
    guards (the dashboard would silently render an empty cell instead of
    telling anyone). Extra keys in a non-default locale are allowed on purpose:
    they are overlays for backend-owned strings (the settings registry docs are
    English in the code and translated here as ``settings.doc.<key>``), which
    the reference catalog deliberately does not duplicate.
    """
    catalogs = {
        locale: json.loads((STRINGS_DIR / f"{locale}.json").read_text(encoding="utf-8"))
        for locale in LOCALES
    }
    reference = set(catalogs[DEFAULT_LOCALE])
    problems = []
    for locale, entries in catalogs.items():
        missing = sorted(reference - set(entries))
        if missing:
            problems.append(f"{locale} missing={missing}")
    if problems:
        raise ValueError(
            "dashboard string catalogs disagree; every locale needs at least the "
            f"reference key set (en has {len(reference)} keys): " + "; ".join(problems)
        )
    return catalogs


_STRINGS = _load_strings()


def render() -> HTMLResponse:
    # ensure_ascii=False bikin teks Indonesia tetep kebaca; "</" di-escape biar
    # label gak bisa nutup script tag tempat dia disisipin.
    strings = json.dumps(_STRINGS, ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8")
    html = html.replace("{{STRINGS_JSON}}", strings)
    html = html.replace("{{DEFAULT_LOCALE}}", DEFAULT_LOCALE)
    html = html.replace("{{APP_VERSION}}", config.VERSION)
    html = html.replace("{{APP_NAME}}", config.APP_NAME)
    html = html.replace("{{CLIENT_BASE}}", f"http://{config.HOST}:{config.PORT}")
    return HTMLResponse(html)
