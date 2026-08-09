"""The tunnel self-test: which hop is broken?

A Cloudflare "502 Bad gateway" lands in the browser during the OAuth callback
and reads like an Etsy failure. It is not — it means Cloudflare could not reach
PrintFlow. These tests pin the diagnosis for each response Cloudflare can give.
"""

from __future__ import annotations

import httpx
import pytest

from app.services import tunnel

pytestmark = pytest.mark.asyncio


async def enable_tunnel(db, hostname="printflow.example.com"):
    await tunnel.save_config(
        db, mode="named", token="tok-abcdef123456", hostname=hostname, enabled=True
    )
    await db.commit()


def mock_transport(monkeypatch, handler):
    """Point the router's HTTP client at a canned response."""
    import app.routers.security_router as router

    real_new_client = router.new_client

    def fake_new_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_new_client(**kwargs)

    monkeypatch.setattr(router, "new_client", fake_new_client)


class TestDiagnosis:
    async def test_without_a_tunnel_there_is_nothing_to_test(self, signed_in):
        body = (await signed_in.post("/api/security/tunnel/test")).json()
        assert body["diagnosis"] == "no_tunnel"
        assert body["ok"] is False

    async def test_healthy_tunnel_reports_ok(self, signed_in, db, monkeypatch):
        await enable_tunnel(db)
        mock_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, json={"ok": True, "features": {"cloudflare_tunnel": True}}
            ),
        )
        body = (await signed_in.post("/api/security/tunnel/test")).json()
        assert body["ok"] is True
        assert body["diagnosis"] == "ok"
        assert body["url"] == "https://printflow.example.com/api/health"

    async def test_502_blames_the_origin_route_not_etsy(self, signed_in, db, monkeypatch):
        await enable_tunnel(db)
        mock_transport(
            monkeypatch,
            lambda request: httpx.Response(502, text="<title>502: Bad gateway</title>"),
        )
        body = (await signed_in.post("/api/security/tunnel/test")).json()
        assert body["ok"] is False
        assert body["diagnosis"] == "origin_unreachable"
        # The actionable part: the exact service value that must be set.
        assert "http://localhost:8000" in body["detail"]
        assert "8443" in body["detail"]

    async def test_530_means_no_connector(self, signed_in, db, monkeypatch):
        await enable_tunnel(db)
        mock_transport(monkeypatch, lambda request: httpx.Response(530, text="error 1033"))
        body = (await signed_in.post("/api/security/tunnel/test")).json()
        assert body["diagnosis"] == "tunnel_down"

    async def test_access_policy_is_not_reported_as_a_fault(self, signed_in, db, monkeypatch):
        await enable_tunnel(db)
        mock_transport(
            monkeypatch,
            lambda request: httpx.Response(403, text="<html>cf-access login</html>"),
        )
        body = (await signed_in.post("/api/security/tunnel/test")).json()
        # Access blocking a server-side probe is expected, not a misconfiguration.
        assert body["ok"] is True
        assert body["diagnosis"] == "access_protected"

    async def test_dns_or_connection_failure_is_reported(self, signed_in, db, monkeypatch):
        await enable_tunnel(db)

        def boom(request):
            raise httpx.ConnectError("nodename nor servname provided")

        mock_transport(monkeypatch, boom)
        body = (await signed_in.post("/api/security/tunnel/test")).json()
        assert body["ok"] is False
        assert body["diagnosis"] == "unreachable"

    async def test_requires_a_session(self, client):
        assert (await client.post("/api/security/tunnel/test")).status_code == 401
