from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .config import get_config
from .crypto import CredentialDecryptionError
from .db import session_scope
from .integrations.base import AuthExpiredError, IntegrationError
from .routers import (
    auth_router,
    backup_router,
    integrations_router,
    calculator_router,
    maintenance_router,
    manufacturing_router,
    orders_router,
    print_jobs_router,
    printers_router,
    products_router,
    security_router,
    setup_router,
    system_router,
)
from .services.credentials import IntegrationNotConfigured
from .services.settings_store import KEY_HTTPS_REDIRECT, get_setting

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("printflow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scheduler.start()
    supervisor = getattr(app.state, "tls_supervisor", None)
    if supervisor is not None:
        await supervisor.start()
    tunnel = getattr(app.state, "tunnel_supervisor", None)
    if tunnel is not None:
        try:
            async with session_scope() as session:
                await tunnel.start_from_db(session)
        except Exception as exc:  # a tunnel problem must not block startup
            log.error("Cloudflare tunnel did not start: %s", exc)
    log.info("PrintFlow started")
    try:
        yield
    finally:
        if tunnel is not None:
            await tunnel.stop()
        if supervisor is not None:
            await supervisor.stop()
        await scheduler.shutdown()


app = FastAPI(title="PrintFlow", version="1.0.0", lifespan=lifespan)

app.include_router(auth_router.router)
app.include_router(setup_router.router)
# Two routers from one module: the signed-in one, and the setup-time one that
# is open only while this install has no admin account.
app.include_router(backup_router.router)
app.include_router(backup_router.setup_router)
app.include_router(security_router.router)
app.include_router(integrations_router.router)
app.include_router(products_router.router)
app.include_router(manufacturing_router.router)
app.include_router(calculator_router.router)
app.include_router(maintenance_router.router)
app.include_router(orders_router.router)
app.include_router(print_jobs_router.router)
app.include_router(printers_router.router)
app.include_router(system_router.router)


# --------------------------------------------------------------------------
# Optional HTTP → HTTPS redirect
# --------------------------------------------------------------------------

_redirect_cache: dict[str, float | bool] = {"value": False, "expires": 0.0}
_REDIRECT_CACHE_TTL = 30.0


def invalidate_https_redirect_cache() -> None:
    _redirect_cache["expires"] = 0.0


async def _https_redirect_enabled() -> bool:
    now = time.monotonic()
    if now < float(_redirect_cache["expires"]):
        return bool(_redirect_cache["value"])
    try:
        async with session_scope() as session:
            value = bool(await get_setting(session, KEY_HTTPS_REDIRECT))
    except Exception:  # a database blip must not break every request
        return False
    _redirect_cache["value"] = value
    _redirect_cache["expires"] = now + _REDIRECT_CACHE_TTL
    return value


@app.middleware("http")
async def no_store_api_responses(request: Request, call_next):
    """API responses are live state — never let a browser or proxy reuse them."""
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.middleware("http")
async def https_redirect(request: Request, call_next):
    if request.url.scheme == "https" or request.url.path == "/api/health":
        return await call_next(request)
    supervisor = getattr(request.app.state, "tls_supervisor", None)
    # Never redirect to a listener that is not actually up — that would strand
    # the operator outside the only UI that can fix the certificate.
    if supervisor is None or not supervisor.running:
        return await call_next(request)
    if not await _https_redirect_enabled():
        return await call_next(request)

    target = request.url.replace(scheme="https", port=get_config().https_port)
    return RedirectResponse(str(target), status_code=307)


@app.exception_handler(IntegrationNotConfigured)
async def _not_configured(request: Request, exc: IntegrationNotConfigured) -> JSONResponse:
    return JSONResponse(
        status_code=409, content={"detail": str(exc), "provider": exc.provider}
    )


@app.exception_handler(AuthExpiredError)
async def _auth_expired(request: Request, exc: AuthExpiredError) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "detail": f"{exc.provider} rejected our credentials. Reconnect it in Settings.",
            "provider": exc.provider,
        },
    )


@app.exception_handler(IntegrationError)
async def _integration_error(request: Request, exc: IntegrationError) -> JSONResponse:
    return JSONResponse(
        status_code=502, content={"detail": str(exc), "provider": exc.provider}
    )


@app.exception_handler(CredentialDecryptionError)
async def _credential_error(
    request: Request, exc: CredentialDecryptionError
) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# --------------------------------------------------------------------------
# Static frontend (single origin in production, so no CORS handling needed)
# --------------------------------------------------------------------------

_static_dir = get_config().static_dir
_index = os.path.join(_static_dir, "index.html")

# index.html must be revalidated on every load. Without an explicit
# Cache-Control browsers fall back to heuristic caching, which can serve a
# stale index.html for hours after an update — and a stale index.html points at
# the *previous* content-hashed bundle, so the app silently keeps running the
# old code even though the container was rebuilt correctly.
NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}
# Asset filenames contain a content hash, so a given URL's bytes never change.
IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


class ImmutableStaticFiles(StaticFiles):
    """Static assets whose URLs are content-hashed, so caching them is safe."""

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        response = super().file_response(*args, **kwargs)
        response.headers.update(IMMUTABLE)
        return response


if os.path.isdir(os.path.join(_static_dir, "assets")):
    app.mount(
        "/assets",
        ImmutableStaticFiles(directory=os.path.join(_static_dir, "assets")),
        name="assets",
    )


@app.get("/{full_path:path}", include_in_schema=False)
async def spa(full_path: str):
    """Serve the built SPA, falling back to index.html for client-side routes."""
    if full_path.startswith("api/"):
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    candidate = os.path.normpath(os.path.join(_static_dir, full_path))
    if (
        full_path
        and candidate.startswith(os.path.abspath(_static_dir))
        and os.path.isfile(candidate)
    ):
        return FileResponse(candidate, headers=NO_CACHE)
    if os.path.isfile(_index):
        return FileResponse(_index, headers=NO_CACHE)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Frontend build not found. Run `npm run build` in ./frontend, "
            "or use the Docker image which builds it for you."
        },
    )
