"""Application entrypoint: `python -m engrix_router.web.server`, or uvicorn.

Who is allowed on which route:
  /health            public (liveness only, no secrets in the body)
  /                  dashboard (HTML; its API still needs the admin token)
  /static/*          dashboard assets
  /v1/*              client api key (the gate can be switched off through env)
  /api/*             EROUTER_ADMIN_TOKEN, fail-closed when the env var is empty

Why the app is assembled here instead of each router building its own FastAPI:
one process, one lifespan, one place to configure CORS -- a router is just an APIRouter.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from engrix_router.core import config
from engrix_router.storage import sqlite as db
from engrix_router.api import (
    admin_connections,
    admin_keys,
    admin_nodes,
    admin_oauth,
    admin_observability,
    admin_proxy,
    admin_settings,
    anthropic,
    openai,
)
from engrix_router.core import logs as applog
from engrix_router.accounts import health
from engrix_router.providers import registry
from engrix_router.services import quota_sync
from engrix_router.storage import settings
from engrix_router.transport import http_client
from engrix_router.web import dashboard

STARTED_AT = time.time()

WEB_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_runtime_dirs()
    db.ensure_ready()
    applog.configure()
    failed = registry.load_errors()
    applog.info(applog.NS_APP, f"engrix-router v{config.VERSION} listening on http://{config.HOST}:{config.PORT}"
                             f" (admin {'ON' if config.ADMIN_IS_CONFIGURED else 'OFF - /api* refuses requests'},"
                             f" client key {'required' if config.REQUIRE_CLIENT_KEY else 'optional'},"
                             f" dry_run={config.DRY_RUN})")
    if not config.ADMIN_IS_CONFIGURED:
        applog.warn(applog.NS_APP, "EROUTER_ADMIN_TOKEN is empty: the admin API is locked. Set the env var first.")
    stop = asyncio.Event()
    task = None
    interval = quota_sync.interval_setting()
    if interval > 0:
        task = asyncio.create_task(quota_sync.run_forever(stop=stop, sleep_s=interval))
        applog.info(applog.NS_APP, f"quota sync started (every {interval}s)")
    for label, error in failed.items():
        applog.warn(applog.NS_PROVIDER, f"provider {label} failed to load and cannot be routed: {error}")
    yield
    if task is not None:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)
    await http_client.aclose_all()
    applog.info(applog.NS_APP, "clean shutdown: HTTP pools closed")


def create_app() -> FastAPI:
    app = FastAPI(title="Engrix Router", version=config.VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    if config.CORS_ALLOW_ORIGINS:
        app.add_middleware(
            CORSMiddleware, allow_origins=config.CORS_ALLOW_ORIGINS, allow_credentials=False,
            allow_methods=["*"], allow_headers=["*"],
        )

    @app.middleware("http")
    async def _revalidate_static(request: Request, call_next):
        # Dashboard = satu proses yang nyajiin file sendiri; heuristic caching
        # browser pernah nyajiin engrix.css lama di atas layout yang udah dibetulin.
        # etag revalidation cuma 304 kalau gak ada yang berubah, jadi no-cache
        # gak mahal dan edit langsung keliatan di reload berikutnya.
        response = await call_next(request)
        if request.url.path.startswith("/static") or request.url.path == "/":
            response.headers["cache-control"] = "no-cache"
        return response

    app.include_router(openai.router)
    app.include_router(anthropic.router)
    app.include_router(admin_keys.router)
    app.include_router(admin_nodes.router)
    app.include_router(admin_connections.router)
    app.include_router(admin_oauth.router)
    app.include_router(admin_proxy.router)
    app.include_router(admin_observability.router)
    app.include_router(admin_settings.router)

    @app.get("/health", tags=["meta"])
    async def healthz() -> dict[str, object]:
        drift = health.drift_state()
        return {
            "ok": True,
            "app": config.APP_NAME,
            "version": config.VERSION,
            "uptime_s": round(time.time() - STARTED_AT, 1),
            "admin_configured": config.ADMIN_IS_CONFIGURED,
            "require_client_key": config.REQUIRE_CLIENT_KEY,
            "dry_run": config.DRY_RUN,
            "upstream_frozen": drift["tripped"],
            "providers": len(registry.definitions()),
            "providers_failed": registry.load_errors(),
            "settings_keys": len(settings.all_settings()),
        }

    @app.get("/", include_in_schema=False)
    async def index():
        return dashboard.render()

    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static"), html=False), name="static")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(f"{__name__}:app", host=config.HOST, port=config.PORT,
                log_level=config.LOG_LEVEL, reload=False)


if __name__ == "__main__":
    main()
