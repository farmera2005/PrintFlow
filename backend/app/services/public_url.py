"""Resolving the one address that OAuth callbacks must come back to.

A redirect URI has to match, character for character, what was registered with
Etsy and Intuit. Deriving it from whichever host the browser happened to use is
how you end up registering `https://192.168.1.50:8443/...` on the LAN and then
failing the moment the callback arrives through Cloudflare.

So when a tunnel is configured, its hostname *is* the public address, and it
wins over everything else. Order:

1. the live tunnel hostname — covers quick tunnels, which only learn their
   hostname once cloudflared connects;
2. the hostname stored with the tunnel configuration;
3. an explicit Public base URL, for people terminating TLS elsewhere;
4. the requesting host, as a last resort.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from . import tunnel
from .settings_store import KEY_PUBLIC_BASE_URL, get_setting

SOURCE_TUNNEL_LIVE = "tunnel_live"
SOURCE_TUNNEL_CONFIG = "tunnel_config"
SOURCE_MANUAL = "manual"
SOURCE_REQUEST = "request"

# Human-readable, for the UI.
SOURCE_LABELS = {
    SOURCE_TUNNEL_LIVE: "the running Cloudflare Tunnel",
    SOURCE_TUNNEL_CONFIG: "your Cloudflare Tunnel hostname",
    SOURCE_MANUAL: "the Public base URL you set",
    SOURCE_REQUEST: "the address you are browsing from",
}


def live_tunnel_hostname(request: Any | None) -> str | None:
    """Hostname from the supervisor, which is the only place a quick tunnel's
    randomly-assigned name exists."""
    if request is None:
        return None
    supervisor = getattr(request.app.state, "tunnel_supervisor", None)
    if supervisor is None or not getattr(supervisor, "running", False):
        return None
    return getattr(supervisor, "hostname", None) or None


async def resolve_base_url(
    session: AsyncSession, request: Any | None = None
) -> tuple[str, str]:
    """Return (base_url, source). Never has a trailing slash."""
    config = await tunnel.load_config(session)
    tunnel_on = bool(config.get("enabled")) and config.get("mode") != tunnel.MODE_OFF

    if tunnel_on:
        live = tunnel.public_url(live_tunnel_hostname(request))
        if live:
            return live, SOURCE_TUNNEL_LIVE
        stored = tunnel.public_url(config.get("hostname"))
        if stored:
            return stored, SOURCE_TUNNEL_CONFIG

    configured = await get_setting(session, KEY_PUBLIC_BASE_URL)
    if configured:
        return str(configured).rstrip("/"), SOURCE_MANUAL

    if request is not None:
        return str(request.base_url).rstrip("/"), SOURCE_REQUEST
    return "", SOURCE_REQUEST


def redirect_uri(base_url: str, provider: str) -> str:
    return f"{base_url.rstrip('/')}/api/integrations/{provider}/callback"


async def redirect_uris(
    session: AsyncSession, request: Any | None = None
) -> dict[str, str]:
    base, _ = await resolve_base_url(session, request)
    return {provider: redirect_uri(base, provider) for provider in ("etsy", "qbo")}
