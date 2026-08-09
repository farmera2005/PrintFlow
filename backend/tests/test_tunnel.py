"""Cloudflare Tunnel: command building, log parsing, supervision, and the API."""

from __future__ import annotations

import asyncio
import os
import stat
import textwrap

import pytest

from app.services import tunnel


class TestCommandBuilding:
    def test_named_tunnel_runs_the_connector(self):
        command = tunnel.build_command("named", binary="/usr/local/bin/cloudflared", local_port=8000)
        assert command[:2] == ["/usr/local/bin/cloudflared", "tunnel"]
        assert command[-1] == "run"
        assert "--no-autoupdate" in command

    def test_quick_tunnel_points_at_the_local_http_port(self):
        command = tunnel.build_command("quick", binary="cloudflared", local_port=8000)
        assert "--url" in command
        assert command[command.index("--url") + 1] == "http://127.0.0.1:8000"

    def test_off_mode_has_no_command(self):
        with pytest.raises(tunnel.TunnelError):
            tunnel.build_command("off", binary="cloudflared", local_port=8000)

    def test_token_never_appears_in_the_argv(self):
        """`ps` is world-readable on most hosts; the token goes in the env."""
        command = tunnel.build_command("named", binary="cloudflared", local_port=8000)
        env = tunnel.build_env("named", "super-secret-token")
        assert "super-secret-token" not in " ".join(command)
        assert env["TUNNEL_TOKEN"] == "super-secret-token"

    def test_named_mode_requires_a_token(self):
        with pytest.raises(tunnel.TunnelError, match="token is required"):
            tunnel.build_env("named", None)

    def test_quick_mode_carries_no_token(self):
        env = tunnel.build_env("quick", None)
        assert "TUNNEL_TOKEN" not in env

    def test_a_stale_token_in_the_environment_is_cleared(self, monkeypatch):
        monkeypatch.setenv("TUNNEL_TOKEN", "left-over-from-the-host")
        assert "TUNNEL_TOKEN" not in tunnel.build_env("quick", None)


class TestLogParsing:
    def test_quick_tunnel_hostname_is_picked_up(self):
        line = "INF |  https://tidy-otter-mango.trycloudflare.com  |"
        assert tunnel.parse_log_line(line)["hostname"] == "tidy-otter-mango.trycloudflare.com"

    def test_registered_connections_are_counted(self):
        line = "INF Registered tunnel connection connIndex=0 location=lhr07"
        assert tunnel.parse_log_line(line)["connection_registered"] is True

    def test_lost_connections_are_noticed(self):
        assert tunnel.parse_log_line("WRN Lost connection with the edge")[
            "connection_lost"
        ] is True

    def test_fatal_errors_surface(self):
        parsed = tunnel.parse_log_line("ERR failed to connect to the edge: token is invalid")
        assert "error" in parsed

    def test_ordinary_chatter_yields_nothing(self):
        assert tunnel.parse_log_line("INF Version 2024.1.0") == {}

    def test_token_is_redacted_from_captured_output(self):
        line = tunnel.redact("using token abcdefghijklmnop now", "abcdefghijklmnop")
        assert "abcdefghijklmnop" not in line
        assert "redacted" in line

    def test_short_values_are_not_redacted_away(self):
        # Guard against blanking half the log because the "token" is "abc".
        assert tunnel.redact("nothing to see", "abc") == "nothing to see"

    def test_public_url_is_built_from_a_bare_hostname(self):
        assert tunnel.public_url("printflow.example.com") == "https://printflow.example.com"
        assert tunnel.public_url(None) is None

    def test_public_url_tolerates_a_pasted_url(self):
        for value in ("https://pf.example.com", "http://pf.example.com/", "pf.example.com/"):
            assert tunnel.public_url(value) == "https://pf.example.com"
        assert tunnel.public_url("   ") is None


@pytest.mark.asyncio
class TestConfigStorage:
    async def test_defaults_to_off(self, db):
        config = await tunnel.load_config(db)
        assert config["mode"] == "off"
        assert config["enabled"] is False

    async def test_round_trip(self, db):
        await tunnel.save_config(
            db, mode="named", token="tok-123456789", hostname="pf.example.com", enabled=True
        )
        await db.commit()
        config = await tunnel.load_config(db)
        assert config["mode"] == "named"
        assert config["token"] == "tok-123456789"
        assert config["hostname"] == "pf.example.com"

    async def test_blank_token_on_edit_keeps_the_stored_one(self, db):
        await tunnel.save_config(
            db, mode="named", token="tok-123456789", hostname="pf.example.com", enabled=True
        )
        await db.commit()
        await tunnel.save_config(
            db, mode="named", token=None, hostname="other.example.com", enabled=True
        )
        await db.commit()
        config = await tunnel.load_config(db)
        assert config["token"] == "tok-123456789"
        assert config["hostname"] == "other.example.com"

    async def test_named_tunnel_without_a_token_is_rejected(self, db):
        with pytest.raises(tunnel.TunnelError, match="connector token"):
            await tunnel.save_config(
                db, mode="named", token=None, hostname="pf.example.com", enabled=True
            )

    async def test_public_config_never_exposes_the_token(self, db):
        await tunnel.save_config(
            db, mode="named", token="tok-abcdef123456", hostname="pf.example.com", enabled=True
        )
        await db.commit()
        public = tunnel.public_config(await tunnel.load_config(db))
        assert "token" not in public
        assert public["has_token"] is True
        assert public["token_hint"] == "…123456"

    async def test_token_is_encrypted_at_rest(self, db):
        from app.services import credentials

        await tunnel.save_config(
            db, mode="named", token="tok-plaintext-check", hostname="a.b", enabled=True
        )
        await db.commit()
        record = await credentials.get_record(db, tunnel.PROVIDER_TUNNEL)
        assert b"tok-plaintext-check" not in bytes(record.encrypted_payload)


def _fake_cloudflared(tmp_path, script: str):
    """A stand-in binary so the supervisor can be exercised for real."""
    path = tmp_path / "cloudflared"
    path.write_text("#!/bin/sh\n" + textwrap.dedent(script))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.mark.asyncio
class TestSupervisor:
    async def test_off_mode_starts_nothing(self):
        supervisor = tunnel.TunnelSupervisor(local_port=8000)
        await supervisor.apply({"mode": "off", "enabled": False, "token": None, "hostname": None})
        assert supervisor.running is False
        assert supervisor.status()["mode"] == "off"

    async def test_missing_binary_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(tunnel, "binary_path", lambda: None)
        supervisor = tunnel.TunnelSupervisor(local_port=8000)
        await supervisor.apply(
            {"mode": "quick", "enabled": True, "token": None, "hostname": None}
        )
        assert supervisor.running is False
        assert "not installed" in supervisor.status()["last_error"]

    async def test_it_runs_captures_output_and_learns_the_hostname(
        self, tmp_path, monkeypatch
    ):
        binary = _fake_cloudflared(
            tmp_path,
            """
            echo "INF Starting tunnel"
            echo "INF |  https://brave-pear-9x.trycloudflare.com  |"
            echo "INF Registered tunnel connection connIndex=0"
            sleep 30
            """,
        )
        monkeypatch.setattr(tunnel, "binary_path", lambda: str(binary))
        supervisor = tunnel.TunnelSupervisor(local_port=8000)
        await supervisor.apply(
            {"mode": "quick", "enabled": True, "token": None, "hostname": None}
        )
        try:
            for _ in range(40):
                await asyncio.sleep(0.1)
                if supervisor.hostname and supervisor.connections:
                    break
            status = supervisor.status()
            assert status["running"] is True
            assert status["hostname"] == "brave-pear-9x.trycloudflare.com"
            assert status["public_url"] == "https://brave-pear-9x.trycloudflare.com"
            assert status["connections"] == 1
            assert any("Starting tunnel" in line for line in supervisor.logs())
        finally:
            await supervisor.stop()
        assert supervisor.running is False

    async def test_stop_terminates_the_process(self, tmp_path, monkeypatch):
        binary = _fake_cloudflared(tmp_path, 'echo "INF up"\nsleep 60\n')
        monkeypatch.setattr(tunnel, "binary_path", lambda: str(binary))
        supervisor = tunnel.TunnelSupervisor(local_port=8000)
        await supervisor.apply(
            {"mode": "quick", "enabled": True, "token": None, "hostname": None}
        )
        await asyncio.sleep(0.3)
        pid = supervisor.status()["pid"]
        assert pid is not None
        await supervisor.stop()
        assert supervisor.running is False
        # The child is gone, not orphaned.
        with pytest.raises(OSError):
            os.kill(pid, 0)

    async def test_a_dead_process_is_restarted(self, tmp_path, monkeypatch):
        binary = _fake_cloudflared(tmp_path, 'echo "INF starting"\nexit 1\n')
        monkeypatch.setattr(tunnel, "binary_path", lambda: str(binary))
        monkeypatch.setattr(tunnel, "RESTART_BACKOFF_SECONDS", (0.1,))
        supervisor = tunnel.TunnelSupervisor(local_port=8000)
        await supervisor.apply(
            {"mode": "quick", "enabled": True, "token": None, "hostname": None}
        )
        try:
            for _ in range(50):
                await asyncio.sleep(0.1)
                if supervisor.restarts:
                    break
            assert supervisor.restarts >= 1
            assert "exited with code 1" in (supervisor.status()["last_error"] or "")
        finally:
            await supervisor.stop()

    async def test_stopping_prevents_further_restarts(self, tmp_path, monkeypatch):
        binary = _fake_cloudflared(tmp_path, "exit 1\n")
        monkeypatch.setattr(tunnel, "binary_path", lambda: str(binary))
        monkeypatch.setattr(tunnel, "RESTART_BACKOFF_SECONDS", (0.1,))
        supervisor = tunnel.TunnelSupervisor(local_port=8000)
        await supervisor.apply(
            {"mode": "quick", "enabled": True, "token": None, "hostname": None}
        )
        await supervisor.stop()
        restarts = supervisor.restarts
        await asyncio.sleep(0.5)
        assert supervisor.restarts == restarts
        assert supervisor.running is False


@pytest.mark.asyncio
class TestTunnelApi:
    async def test_status_is_included_in_the_security_payload(self, signed_in):
        body = (await signed_in.get("/api/security")).json()
        assert body["tunnel"]["mode"] == "off"
        assert body["tunnel"]["has_token"] is False
        assert "status" in body["tunnel"]

    async def test_requires_a_session(self, client):
        assert (await client.post("/api/security/tunnel", json={"mode": "off"})).status_code == 401

    async def test_configuring_a_named_tunnel_stores_it_redacted(self, signed_in):
        response = await signed_in.post(
            "/api/security/tunnel",
            json={
                "mode": "named",
                "enabled": True,
                "token": "eyJhIjoidGVzdCJ9-longtoken",
                "hostname": "printflow.example.com",
            },
        )
        assert response.status_code == 200, response.text
        info = response.json()["tunnel"]
        assert info["mode"] == "named"
        assert info["hostname"] == "printflow.example.com"
        assert info["has_token"] is True
        assert "token" not in info
        assert "eyJhIjoidGVzdCJ9-longtoken" not in response.text

    async def test_named_tunnel_without_a_token_is_rejected(self, signed_in):
        response = await signed_in.post(
            "/api/security/tunnel",
            json={"mode": "named", "enabled": True, "hostname": "pf.example.com"},
        )
        assert response.status_code == 400
        assert "connector token" in response.json()["detail"]

    async def test_unknown_mode_is_rejected(self, signed_in):
        response = await signed_in.post("/api/security/tunnel", json={"mode": "ngrok"})
        assert response.status_code == 422

    async def test_stop_disables_without_forgetting_the_token(self, signed_in):
        await signed_in.post(
            "/api/security/tunnel",
            json={"mode": "named", "enabled": True, "token": "tok-abcdef123456", "hostname": "a.b"},
        )
        body = (await signed_in.post("/api/security/tunnel/stop")).json()
        assert body["tunnel"]["enabled"] is False
        assert body["tunnel"]["has_token"] is True

    async def test_restart_without_a_supervisor_is_a_clear_409(self, signed_in):
        response = await signed_in.post("/api/security/tunnel/restart")
        assert response.status_code == 409

    async def test_logs_endpoint_is_empty_without_a_supervisor(self, signed_in):
        assert (await signed_in.get("/api/security/tunnel/logs")).json() == {"lines": []}

    async def test_tunnel_config_is_audited_without_the_token(self, signed_in, db):
        from sqlalchemy import select

        from app.models import AuditLog

        await signed_in.post(
            "/api/security/tunnel",
            json={"mode": "named", "enabled": True, "token": "tok-secret-12345", "hostname": "a.b"},
        )
        entries = (await db.execute(select(AuditLog))).scalars().all()
        entry = next(e for e in entries if e.action == "tunnel_configured")
        assert entry.detail["hostname"] == "a.b"
        assert "tok-secret-12345" not in str(entry.detail)

    async def test_tunnel_is_not_one_of_the_four_integrations(self, signed_in):
        """It is infrastructure — it must not show up as a fifth platform."""
        await signed_in.post(
            "/api/security/tunnel",
            json={"mode": "named", "enabled": True, "token": "tok-abcdef123456", "hostname": "a.b"},
        )
        body = (await signed_in.get("/api/settings")).json()
        providers = {row["provider"] for row in body["integrations"]}
        assert providers == {"etsy", "qbo", "bambuddy", "shipstation"}

        steps = (await signed_in.get("/api/setup/status")).json()["steps"]
        assert all(step["key"] != tunnel.PROVIDER_TUNNEL for step in steps)


@pytest.mark.asyncio
class TestSessionCookieOverHttps:
    async def test_cookie_is_not_secure_on_plain_http(self, client):
        response = await client.post(
            "/api/setup/admin", json={"username": "admin", "password": "hunter2hunter2"}
        )
        # Marking it Secure here would break sign-in on the LAN fallback port.
        assert "secure" not in response.headers["set-cookie"].lower()

    async def test_cookie_is_secure_when_the_request_is_https(self, db):
        from httpx import ASGITransport, AsyncClient

        from app.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://printflow.example.com") as http:
            response = await http.post(
                "/api/setup/admin", json={"username": "admin", "password": "hunter2hunter2"}
            )
        assert response.status_code == 200
        assert "secure" in response.headers["set-cookie"].lower()
