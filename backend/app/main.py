from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import scheduler
from .config import get_config
from .crypto import CredentialDecryptionError
from .integrations.base import AuthExpiredError, IntegrationError
from .routers import (
    auth_router,
    integrations_router,
    orders_router,
    print_jobs_router,
    products_router,
    setup_router,
    system_router,
)
from .services.credentials import IntegrationNotConfigured

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("printflow")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await scheduler.start()
    log.info("PrintFlow started")
    try:
        yield
    finally:
        await scheduler.shutdown()


app = FastAPI(title="PrintFlow", version="1.0.0", lifespan=lifespan)

app.include_router(auth_router.router)
app.include_router(setup_router.router)
app.include_router(integrations_router.router)
app.include_router(products_router.router)
app.include_router(orders_router.router)
app.include_router(print_jobs_router.router)
app.include_router(system_router.router)


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

if os.path.isdir(os.path.join(_static_dir, "assets")):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_static_dir, "assets")),
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
        return FileResponse(candidate)
    if os.path.isfile(_index):
        return FileResponse(_index)
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Frontend build not found. Run `npm run build` in ./frontend, "
            "or use the Docker image which builds it for you."
        },
    )
