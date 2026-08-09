"""OAuth callback URLs must follow the tunnel, not the browsing address.

A redirect URI has to match what was registered with Etsy and Intuit exactly.
Deriving it from whichever host the operator happened to browse from is how you
register a LAN address and then fail when the callback arrives via Cloudflare.
"""

from __future__ import annotations

import pytest

from app.services import public_url, tunnel
from app.services.settings_store import KEY_PUBLIC_BASE_URL, set_setting

pytestmark = pytest.mark.asyncio


class FakeSupervisor:
    def __init__(self, hostname=None, running=True):
        self.hostname = hostname
        self.running = running


class FakeRequest:
    """Minimal stand-in: a base_url and an app.state carrying the supervisor."""

    def __init__(self, base_url="https://192.168.1.50:8443/", supervisor=None):
        self.base_url = base_url
        self.app = type("App", (), {"state": type("State", (), {})()})()
        if supervisor is not None:
            self.app.state.tunnel_supervisor = supervisor


async def enable_named_tunnel(db, hostname="printflow.example.com"):
    await tunnel.save_config(
        db, mode="named", token="tok-abcdef123456", hostname=hostname, enabled=True
    )
    await db.commit()


class TestResolutionOrder:
    async def test_falls_back_to_the_requesting_host(self, db):
        url, source = await public_url.resolve_base_url(db, FakeRequest())
        assert url == "https://192.168.1.50:8443"
        assert source == public_url.SOURCE_REQUEST

    async def test_manual_base_url_beats_the_requesting_host(self, db):
        await set_setting(db, KEY_PUBLIC_BASE_URL, "https://printflow.mynas.local")
        await db.commit()
        url, source = await public_url.resolve_base_url(db, FakeRequest())
        assert url == "https://printflow.mynas.local"
        assert source == public_url.SOURCE_MANUAL

    async def test_configured_tunnel_beats_the_requesting_host(self, db):
        await enable_named_tunnel(db)
        url, source = await public_url.resolve_base_url(db, FakeRequest())
        assert url == "https://printflow.example.com"
        assert source == public_url.SOURCE_TUNNEL_CONFIG

    async def test_tunnel_beats_a_stale_manual_base_url(self, db):
        # The exact footgun: a LAN address left over from before the tunnel.
        await set_setting(db, KEY_PUBLIC_BASE_URL, "https://192.168.1.50:8443")
        await enable_named_tunnel(db)
        url, source = await public_url.resolve_base_url(db, FakeRequest())
        assert url == "https://printflow.example.com"
        assert source == public_url.SOURCE_TUNNEL_CONFIG

    async def test_live_hostname_beats_the_stored_one(self, db):
        """A quick tunnel only learns its hostname once cloudflared connects."""
        await tunnel.save_config(db, mode="quick", token=None, hostname=None, enabled=True)
        await db.commit()
        request = FakeRequest(supervisor=FakeSupervisor("brave-pear-9x.trycloudflare.com"))
        url, source = await public_url.resolve_base_url(db, request)
        assert url == "https://brave-pear-9x.trycloudflare.com"
        assert source == public_url.SOURCE_TUNNEL_LIVE

    async def test_a_stopped_supervisor_is_ignored(self, db):
        await enable_named_tunnel(db)
        request = FakeRequest(supervisor=FakeSupervisor("stale.example.com", running=False))
        url, source = await public_url.resolve_base_url(db, request)
        assert url == "https://printflow.example.com"
        assert source == public_url.SOURCE_TUNNEL_CONFIG

    async def test_disabled_tunnel_does_not_hijack_the_address(self, db):
        await enable_named_tunnel(db)
        config = await tunnel.load_config(db)
        await tunnel.save_config(
            db, mode=config["mode"], token=None, hostname=config["hostname"], enabled=False
        )
        await set_setting(db, KEY_PUBLIC_BASE_URL, "https://printflow.mynas.local")
        await db.commit()
        url, source = await public_url.resolve_base_url(db, FakeRequest())
        assert url == "https://printflow.mynas.local"
        assert source == public_url.SOURCE_MANUAL

    async def test_never_returns_a_trailing_slash(self, db):
        await set_setting(db, KEY_PUBLIC_BASE_URL, "https://printflow.example.com/")
        await db.commit()
        url, _ = await public_url.resolve_base_url(db, FakeRequest())
        assert url == "https://printflow.example.com"

    async def test_redirect_uris_follow_the_tunnel(self, db):
        await enable_named_tunnel(db)
        uris = await public_url.redirect_uris(db, FakeRequest())
        assert uris == {
            "etsy": "https://printflow.example.com/api/integrations/etsy/callback",
            "qbo": "https://printflow.example.com/api/integrations/qbo/callback",
        }


class TestThroughTheApi:
    async def test_integrations_redirect_uris_use_the_tunnel(self, signed_in, db):
        await enable_named_tunnel(db)
        body = (await signed_in.get("/api/integrations")).json()
        assert body["base_url"] == "https://printflow.example.com"
        assert (
            body["redirect_uris"]["etsy"]
            == "https://printflow.example.com/api/integrations/etsy/callback"
        )

    async def test_security_payload_reports_the_source(self, signed_in, db):
        await enable_named_tunnel(db)
        body = (await signed_in.get("/api/security")).json()
        assert body["effective_base_url"] == "https://printflow.example.com"
        assert body["base_url_source"] == "tunnel_config"
        assert "Cloudflare Tunnel" in body["base_url_source_label"]
        assert body["redirect_uris"]["qbo"].startswith("https://printflow.example.com/")

    async def test_the_authorize_url_carries_the_tunnel_redirect(self, signed_in, db):
        """The URI sent to Etsy must be the one that was registered."""
        await enable_named_tunnel(db)
        response = await signed_in.post(
            "/api/integrations/etsy/start",
            json={"keystring": "keystring-value", "shared_secret": "secret-value"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert (
            body["redirect_uri"]
            == "https://printflow.example.com/api/integrations/etsy/callback"
        )
        assert "printflow.example.com" in body["authorize_url"]

    async def test_qbo_authorize_url_too(self, signed_in, db):
        await enable_named_tunnel(db)
        response = await signed_in.post(
            "/api/integrations/qbo/start",
            json={"client_id": "client-id-value", "client_secret": "client-secret-value"},
        )
        assert response.status_code == 200, response.text
        assert (
            response.json()["redirect_uri"]
            == "https://printflow.example.com/api/integrations/qbo/callback"
        )

    async def test_turning_the_tunnel_off_restores_the_manual_address(self, signed_in, db):
        await signed_in.post("/api/security/base-url", json={"public_base_url": "https://nas.local"})
        await enable_named_tunnel(db)
        assert (await signed_in.get("/api/security")).json()["effective_base_url"] == (
            "https://printflow.example.com"
        )

        await signed_in.post("/api/security/tunnel", json={"mode": "off", "enabled": False})
        body = (await signed_in.get("/api/security")).json()
        assert body["effective_base_url"] == "https://nas.local"
        assert body["base_url_source"] == "manual"
