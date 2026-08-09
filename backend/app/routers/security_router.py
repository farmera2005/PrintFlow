"""Access and security: public base URL, HTTPS certificate, HTTP redirect.

All of this is configured during first-run setup — there is nothing to generate
by hand before installing.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..config import get_config
from ..db import get_session
from ..integrations.base import new_client
from ..models import User
from ..services import audit, public_url, tls, tunnel
from ..services.settings_store import (
    KEY_HTTPS_REDIRECT,
    KEY_PUBLIC_BASE_URL,
    get_setting,
    set_setting,
)

router = APIRouter(prefix="/api/security", tags=["security"])


def _supervisor(request: Request):
    return getattr(request.app.state, "tls_supervisor", None)


def _tunnel(request: Request):
    return getattr(request.app.state, "tunnel_supervisor", None)


def _reload_listener(request: Request) -> dict[str, Any]:
    """Apply a new certificate without a restart, if we own the HTTPS listener.

    The swap is deferred until just after this response goes out — this request
    is very likely being served by the listener that is about to be replaced.
    """
    supervisor = _supervisor(request)
    if supervisor is None:
        return {
            "applied": False,
            "detail": (
                "Saved. The HTTPS listener is not managed by this process "
                "(development mode) — restart to apply."
            ),
        }
    supervisor.schedule_reload()
    return {
        "applied": True,
        "detail": (
            "Saved. HTTPS is restarting on the new certificate — your browser will "
            "ask you to trust it again."
        ),
    }


async def _payload(request: Request, session: AsyncSession) -> dict[str, Any]:
    config = get_config()
    row = await tls.active_certificate(session)
    certificate = None
    if row is not None:
        try:
            certificate = {**tls.describe(row.cert_pem), "source": row.source}
        except tls.CertificateError:
            certificate = None

    supervisor = _supervisor(request)
    base_url = await get_setting(session, KEY_PUBLIC_BASE_URL)
    host = request.url.hostname or "localhost"

    tunnel_config = await tunnel.load_config(session)
    tunnel_supervisor = _tunnel(request)
    tunnel_status = (
        tunnel_supervisor.status()
        if tunnel_supervisor is not None
        else {"binary_available": tunnel.available(), "running": False, "supervised": False}
    )

    # The address OAuth callbacks actually resolve to, and the exact URIs that
    # have to be registered with Etsy and Intuit.
    effective_base_url, base_url_source = await public_url.resolve_base_url(session, request)
    return {
        "certificate": certificate,
        "effective_base_url": effective_base_url,
        "base_url_source": base_url_source,
        "base_url_source_label": public_url.SOURCE_LABELS.get(base_url_source, ""),
        "redirect_uris": await public_url.redirect_uris(session, request),
        "tunnel": {
            **tunnel.public_config(tunnel_config),
            "status": tunnel_status,
            "supervised": tunnel_supervisor is not None,
        },
        "https": (supervisor.status() if supervisor else {"running": False, "port": config.https_port}),
        "https_port": config.https_port,
        "http_port": config.http_port,
        "https_redirect": bool(await get_setting(session, KEY_HTTPS_REDIRECT)),
        "public_base_url": base_url,
        "suggested_hosts": tls.local_hostnames(),
        "current_host": host,
        "suggested_base_url": base_url or f"https://{host}:{config.https_port}",
        "secret_key_source": config.secret_key_source,
        "data_dir": str(config.data_dir),
        "default_validity_days": tls.DEFAULT_VALIDITY_DAYS,
    }


@router.get("")
async def security_status(
    request: Request,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await _payload(request, session)


class GenerateRequest(BaseModel):
    hosts: list[str] = Field(default_factory=list)
    validity_days: int = Field(default=tls.DEFAULT_VALIDITY_DAYS, ge=1, le=tls.MAX_VALIDITY_DAYS)


@router.post("/certificate/generate")
async def generate_certificate(
    body: GenerateRequest,
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    hosts = body.hosts or tls.local_hostnames()
    hostnames, addresses = tls.split_hosts(hosts)
    if not hostnames and not addresses:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Enter at least one hostname or IP address this server is reached on.",
        )
    cert_pem, key_pem = tls.generate_self_signed(
        hosts=hosts, validity_days=body.validity_days
    )
    row = await tls.save(session, cert_pem, key_pem, source="generated")
    await audit.record(
        session,
        entity_type="tls_certificate",
        entity_id=row.id,
        action="certificate_generated",
        detail={"hosts": hostnames + addresses, "validity_days": body.validity_days},
        actor=user.username,
    )
    await session.commit()
    result = _reload_listener(request)
    return {**await _payload(request, session), "result": result}


class UploadRequest(BaseModel):
    cert_pem: str = Field(min_length=1)
    key_pem: str = Field(min_length=1)


@router.post("/certificate/upload")
async def upload_certificate(
    body: UploadRequest,
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Use your own certificate instead — validated before it is stored."""
    try:
        row = await tls.save(
            session, body.cert_pem.strip(), body.key_pem.strip(), source="uploaded"
        )
    except tls.CertificateError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await audit.record(
        session,
        entity_type="tls_certificate",
        entity_id=row.id,
        action="certificate_uploaded",
        detail={"fingerprint": row.fingerprint_sha256},
        actor=user.username,
    )
    await session.commit()
    result = _reload_listener(request)
    return {**await _payload(request, session), "result": result}


@router.get("/certificate.crt")
async def download_certificate(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> Response:
    """The public certificate, for adding to a browser or OS trust store."""
    row = await tls.active_certificate(session)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No certificate has been generated yet")
    return Response(
        content=row.cert_pem,
        media_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="printflow.crt"'},
    )


class BaseUrlRequest(BaseModel):
    public_base_url: str | None = None


@router.post("/base-url")
async def save_base_url(
    body: BaseUrlRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    value = (body.public_base_url or "").strip().rstrip("/") or None
    if value and not value.startswith(("http://", "https://")):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "The base URL must start with http:// or https://"
        )
    await set_setting(session, KEY_PUBLIC_BASE_URL, value)
    await session.commit()
    return {"public_base_url": value}


# --------------------------------------------------------------------------
# Cloudflare Tunnel
# --------------------------------------------------------------------------


class TunnelRequest(BaseModel):
    mode: str = Field(pattern="^(off|named|quick)$")
    enabled: bool = True
    # Blank on an edit means "keep the stored token".
    token: str | None = None
    hostname: str | None = None


@router.post("/tunnel")
async def configure_tunnel(
    body: TunnelRequest,
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Save the tunnel configuration and bring cloudflared in line with it."""
    try:
        config = await tunnel.save_config(
            session,
            mode=body.mode,
            token=body.token,
            hostname=body.hostname,
            enabled=body.enabled,
        )
    except tunnel.TunnelError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await audit.record(
        session,
        entity_type="tunnel",
        entity_id=None,
        action="tunnel_configured",
        # Never the token.
        detail={"mode": config["mode"], "enabled": config["enabled"], "hostname": config["hostname"]},
        actor=user.username,
    )
    await session.commit()

    supervisor = _tunnel(request)
    if supervisor is not None:
        await supervisor.apply(config)
    return await _payload(request, session)


@router.post("/tunnel/restart")
async def restart_tunnel(
    request: Request,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    supervisor = _tunnel(request)
    if supervisor is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The tunnel is not supervised by this process (development mode).",
        )
    await supervisor.restart()
    return await _payload(request, session)


@router.post("/tunnel/stop")
async def stop_tunnel(
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    config = await tunnel.load_config(session)
    await tunnel.save_config(
        session,
        mode=config["mode"],
        token=None,
        hostname=config["hostname"],
        enabled=False,
    )
    await audit.record(
        session,
        entity_type="tunnel",
        entity_id=None,
        action="tunnel_stopped",
        actor=user.username,
    )
    await session.commit()
    supervisor = _tunnel(request)
    if supervisor is not None:
        await supervisor.stop()
    return await _payload(request, session)


@router.post("/tunnel/test")
async def test_tunnel(
    request: Request,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fetch our own public URL and say which hop is broken.

    A Cloudflare 502 on the callback looks like an Etsy failure but means
    Cloudflare could not reach *us*, which is almost always the tunnel's public
    hostname pointing at the wrong local address.
    """
    base, source = await public_url.resolve_base_url(session, request)
    if source not in (public_url.SOURCE_TUNNEL_LIVE, public_url.SOURCE_TUNNEL_CONFIG):
        return {
            "url": base,
            "ok": False,
            "status": None,
            "diagnosis": "no_tunnel",
            "detail": "No tunnel hostname is configured, so there is nothing to test.",
        }

    url = f"{base}/api/health"
    try:
        async with new_client(timeout=httpx.Timeout(15.0)) as client:
            response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        return {
            "url": url,
            "ok": False,
            "status": None,
            "diagnosis": "unreachable",
            "detail": (
                f"Could not reach {base} at all ({type(exc).__name__}). Either DNS for "
                "that hostname does not resolve, or the tunnel is not connected."
            ),
        }

    body = response.text[:400]
    if response.status_code == 200 and "cloudflare_tunnel" in body:
        return {
            "url": url,
            "ok": True,
            "status": 200,
            "diagnosis": "ok",
            "detail": "Cloudflare reached PrintFlow successfully. Callback URLs will work.",
        }
    if response.status_code in (502, 503, 504):
        return {
            "url": url,
            "ok": False,
            "status": response.status_code,
            "diagnosis": "origin_unreachable",
            "detail": (
                "Cloudflare answered but could not reach PrintFlow — this is the "
                "'Bad gateway' page. In the tunnel's Public Hostname settings the "
                "service must be exactly http://localhost:8000 — plain HTTP, port "
                "8000. Pointing it at https:// or at port 8443 fails, because that "
                "port serves a self-signed certificate Cloudflare will not trust."
            ),
        }
    if response.status_code in (530, 521, 523):
        return {
            "url": url,
            "ok": False,
            "status": response.status_code,
            "diagnosis": "tunnel_down",
            "detail": (
                "Cloudflare has no connector for this hostname. The tunnel is not "
                "connected — check the log below, and that the token belongs to the "
                "tunnel this hostname is routed through."
            ),
        }
    if response.status_code in (401, 403) or "cf-access" in body.lower():
        return {
            "url": url,
            "ok": True,
            "status": response.status_code,
            "diagnosis": "access_protected",
            "detail": (
                "Cloudflare Access is protecting this hostname, so this server-side "
                "check is blocked. That is expected and fine — browsers sign in at "
                "Cloudflare first, and the OAuth callback still works."
            ),
        }
    return {
        "url": url,
        "ok": False,
        "status": response.status_code,
        "diagnosis": "unexpected",
        "detail": f"Unexpected response {response.status_code} from {base}.",
    }


@router.get("/tunnel/logs")
async def tunnel_logs(
    request: Request,
    limit: int = 100,
    _: User = Depends(require_user),
) -> dict:
    supervisor = _tunnel(request)
    if supervisor is None:
        return {"lines": []}
    return {"lines": supervisor.logs(max(1, min(limit, 300)))}


class RedirectRequest(BaseModel):
    enabled: bool


@router.post("/https-redirect")
async def set_https_redirect(
    body: RedirectRequest,
    request: Request,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    supervisor = _supervisor(request)
    if body.enabled and supervisor is not None and not supervisor.running:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "HTTPS is not currently listening, so redirecting to it would lock you out. "
            "Generate a working certificate first.",
        )
    await set_setting(session, KEY_HTTPS_REDIRECT, body.enabled)
    await audit.record(
        session,
        entity_type="settings",
        entity_id=None,
        action="https_redirect_enabled" if body.enabled else "https_redirect_disabled",
        actor=user.username,
    )
    await session.commit()

    from ..main import invalidate_https_redirect_cache

    invalidate_https_redirect_cache()
    return {"https_redirect": body.enabled}
