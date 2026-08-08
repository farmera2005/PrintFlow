"""Secret-key bootstrap, certificate generation, and the security endpoints."""

from __future__ import annotations

import datetime
import os
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.config import SECRET_KEY_FILENAME, Config, _resolve_secret_key
from app.services import tls


class TestSecretKeyBootstrap:
    """`docker compose up -d` must work with no SECRET_KEY set anywhere."""

    def test_environment_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "from-the-environment")
        key, source = _resolve_secret_key(tmp_path)
        assert (key, source) == ("from-the-environment", "environment")
        assert not (tmp_path / SECRET_KEY_FILENAME).exists()

    def test_generated_and_persisted_on_first_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        key, source = _resolve_secret_key(tmp_path)
        assert source == "generated"
        assert len(key) >= 40
        assert (tmp_path / SECRET_KEY_FILENAME).read_text() == key

    def test_second_run_reuses_the_same_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        first, _ = _resolve_secret_key(tmp_path)
        second, source = _resolve_secret_key(tmp_path)
        # A rotated key would make every stored credential undecryptable.
        assert second == first
        assert source == "file"

    def test_key_file_is_not_world_readable(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        _resolve_secret_key(tmp_path)
        mode = stat.S_IMODE(os.stat(tmp_path / SECRET_KEY_FILENAME).st_mode)
        assert mode == 0o600

    def test_blank_environment_value_is_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "   ")
        _, source = _resolve_secret_key(tmp_path)
        assert source == "generated"

    def test_unwritable_data_dir_fails_loudly(self, tmp_path, monkeypatch):
        # Silently falling back to an in-memory key would look fine until the
        # first restart, when every stored credential stops decrypting.
        monkeypatch.delenv("SECRET_KEY", raising=False)

        def refuse(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(os, "open", refuse)
        with pytest.raises(RuntimeError, match="stable key"):
            _resolve_secret_key(tmp_path)

    def test_concurrent_first_boot_agrees_on_one_key(self, tmp_path, monkeypatch):
        """Two workers racing must not each persist a different key."""
        monkeypatch.delenv("SECRET_KEY", raising=False)
        real_open = os.open
        winner: dict[str, str] = {}

        def race(path, flags, mode=0o777):
            # Simulate another process winning the O_EXCL create first.
            if not winner:
                winner["key"] = "written-by-the-other-worker"
                Path(path).write_text(winner["key"])
            return real_open(path, flags, mode)

        monkeypatch.setattr(os, "open", race)
        key, source = _resolve_secret_key(tmp_path)
        assert key == winner["key"]
        assert source == "file"

    def test_config_resolves_a_data_dir_and_key(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("PRINTFLOW_DATA_DIR", str(tmp_path / "data"))
        config = Config()
        assert config.data_dir == (tmp_path / "data").resolve()
        assert config.secret_key_source == "generated"
        assert config.https_port == 8443


class TestHostParsing:
    def test_separates_names_from_addresses(self):
        hostnames, addresses = tls.split_hosts(["nas.local", "192.168.1.50", "::1"])
        assert hostnames == ["nas.local"]
        assert addresses == ["192.168.1.50", "::1"]

    def test_tolerates_urls_and_ports(self):
        hostnames, addresses = tls.split_hosts(
            ["https://nas.local:8443", "http://10.0.0.4:8000/", "  "]
        )
        assert hostnames == ["nas.local"]
        assert addresses == ["10.0.0.4"]

    def test_deduplicates(self):
        hostnames, _ = tls.split_hosts(["nas.local", "nas.local"])
        assert hostnames == ["nas.local"]

    def test_local_hostnames_always_include_loopback(self):
        suggested = tls.local_hostnames()
        assert "localhost" in suggested
        assert "127.0.0.1" in suggested


class TestGeneration:
    def test_certificate_covers_the_requested_names(self):
        cert_pem, key_pem = tls.generate_self_signed(
            hosts=["nas.local", "192.168.1.50"], validity_days=30
        )
        details = tls.describe(cert_pem)
        assert "nas.local" in details["sans"]
        assert "192.168.1.50" in details["sans"]
        # localhost/loopback are always added so local access keeps working.
        assert "localhost" in details["sans"]
        assert "127.0.0.1" in details["sans"]
        assert details["self_signed"] is True
        assert not details["expired"]
        assert 28 <= details["days_remaining"] <= 30
        assert key_pem.startswith("-----BEGIN PRIVATE KEY-----")

    def test_certificate_is_backdated_against_clock_skew(self):
        cert_pem, _ = tls.generate_self_signed(hosts=["nas.local"])
        certificate = x509.load_pem_x509_certificate(cert_pem.encode())
        not_before = getattr(certificate, "not_valid_before_utc", None) or (
            certificate.not_valid_before.replace(tzinfo=datetime.timezone.utc)
        )
        assert not_before < datetime.datetime.now(datetime.timezone.utc)

    def test_it_is_a_server_certificate_not_a_ca(self):
        cert_pem, _ = tls.generate_self_signed(hosts=["nas.local"])
        certificate = x509.load_pem_x509_certificate(cert_pem.encode())
        basic = certificate.extensions.get_extension_for_class(x509.BasicConstraints).value
        assert basic.ca is False
        eku = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku

    def test_validity_is_clamped(self):
        cert_pem, _ = tls.generate_self_signed(hosts=["a.local"], validity_days=99_999)
        assert tls.describe(cert_pem)["days_remaining"] <= tls.MAX_VALIDITY_DAYS

    def test_each_generation_is_unique(self):
        first, _ = tls.generate_self_signed(hosts=["a.local"])
        second, _ = tls.generate_self_signed(hosts=["a.local"])
        assert tls.describe(first)["fingerprint_sha256"] != tls.describe(second)[
            "fingerprint_sha256"
        ]


class TestValidation:
    def test_matching_pair_is_accepted(self):
        cert_pem, key_pem = tls.generate_self_signed(hosts=["a.local"])
        tls.validate_pair(cert_pem, key_pem)  # does not raise

    def test_mismatched_key_is_rejected(self):
        cert_pem, _ = tls.generate_self_signed(hosts=["a.local"])
        _, other_key = tls.generate_self_signed(hosts=["b.local"])
        with pytest.raises(tls.CertificateError, match="does not match"):
            tls.validate_pair(cert_pem, other_key)

    def test_garbage_certificate_is_rejected(self):
        _, key_pem = tls.generate_self_signed(hosts=["a.local"])
        with pytest.raises(tls.CertificateError, match="PEM certificate"):
            tls.validate_pair("not a certificate", key_pem)

    def test_garbage_key_is_rejected(self):
        cert_pem, _ = tls.generate_self_signed(hosts=["a.local"])
        with pytest.raises(tls.CertificateError, match="PEM private key"):
            tls.validate_pair(cert_pem, "not a key")

    def test_passphrase_protected_key_is_rejected_with_advice(self):
        key = ec.generate_private_key(ec.SECP256R1())
        encrypted = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.BestAvailableEncryption(b"hunter2"),
        ).decode()
        with pytest.raises(tls.CertificateError, match="passphrase"):
            tls.load_private_key(encrypted)


class TestMaterialize:
    def test_files_are_written_private(self, tmp_path):
        cert_pem, key_pem = tls.generate_self_signed(hosts=["a.local"])
        cert_path, key_path = tls.materialize(cert_pem, key_pem, tmp_path / "tls")
        assert cert_path.read_text() == cert_pem
        assert key_path.read_text() == key_pem
        assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600

    def test_rewriting_replaces_the_previous_pair(self, tmp_path: Path):
        directory = tmp_path / "tls"
        first_cert, first_key = tls.generate_self_signed(hosts=["a.local"])
        tls.materialize(first_cert, first_key, directory)
        second_cert, second_key = tls.generate_self_signed(hosts=["b.local"])
        cert_path, _ = tls.materialize(second_cert, second_key, directory)
        assert cert_path.read_text() == second_cert


# --------------------------------------------------------------------------
# Storage + HTTP API
# --------------------------------------------------------------------------

pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
class TestStorage:
    async def test_ensure_generates_once_then_reuses(self, db):
        first = await tls.ensure_certificate(db, hosts=["nas.local"])
        await db.commit()
        second = await tls.ensure_certificate(db)
        assert second.id == first.id

    async def test_saving_deactivates_the_previous_certificate(self, db):
        first = await tls.ensure_certificate(db, hosts=["nas.local"])
        await db.commit()
        cert_pem, key_pem = tls.generate_self_signed(hosts=["new.local"])
        second = await tls.save(db, cert_pem, key_pem)
        await db.commit()

        active = await tls.active_certificate(db)
        assert active.id == second.id
        await db.refresh(first)
        assert first.active is False

    async def test_private_key_is_encrypted_at_rest(self, db):
        row = await tls.ensure_certificate(db, hosts=["nas.local"])
        await db.commit()
        assert b"PRIVATE KEY" not in bytes(row.encrypted_key)
        assert tls.private_key_of(row).startswith("-----BEGIN PRIVATE KEY-----")


@pytest.mark.asyncio
class TestSecurityApi:
    async def test_endpoints_require_a_session(self, client):
        assert (await client.get("/api/security")).status_code == 401

    async def test_setup_lists_security_before_the_oauth_steps(self, signed_in):
        body = (await signed_in.get("/api/setup/status")).json()
        keys = [step["key"] for step in body["steps"]]
        assert keys[:3] == ["admin", "security", "etsy"]
        # Nothing generated yet in a bare test database.
        assert body["steps"][1]["complete"] is False

    async def test_status_suggests_hosts_and_reports_the_key_source(self, signed_in):
        body = (await signed_in.get("/api/security")).json()
        assert body["certificate"] is None
        assert body["https_port"] == 8443
        assert body["https_redirect"] is False
        assert "localhost" in body["suggested_hosts"]
        assert body["secret_key_source"] == "environment"

    async def test_generating_a_certificate_completes_the_step(self, signed_in):
        response = await signed_in.post(
            "/api/security/certificate/generate",
            json={"hosts": ["nas.local", "192.168.1.50"], "validity_days": 90},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "nas.local" in body["certificate"]["sans"]
        assert body["certificate"]["source"] == "generated"
        # No supervisor in tests, so it reports "restart to apply" rather than lying.
        assert body["result"]["applied"] is False

        status = (await signed_in.get("/api/setup/status")).json()
        security = next(s for s in status["steps"] if s["key"] == "security")
        assert security["complete"] is True

    async def test_generating_with_no_hosts_falls_back_to_local_names(self, signed_in):
        response = await signed_in.post(
            "/api/security/certificate/generate", json={"hosts": []}
        )
        assert response.status_code == 200
        assert "localhost" in response.json()["certificate"]["sans"]

    async def test_blank_hosts_are_rejected(self, signed_in):
        response = await signed_in.post(
            "/api/security/certificate/generate", json={"hosts": ["  ", ""]}
        )
        assert response.status_code == 400

    async def test_certificate_download_returns_only_the_public_half(self, signed_in):
        await signed_in.post("/api/security/certificate/generate", json={"hosts": ["a.local"]})
        response = await signed_in.get("/api/security/certificate.crt")
        assert response.status_code == 200
        assert response.text.startswith("-----BEGIN CERTIFICATE-----")
        assert "PRIVATE KEY" not in response.text
        assert "attachment" in response.headers["content-disposition"]

    async def test_download_before_generation_is_404(self, signed_in):
        assert (await signed_in.get("/api/security/certificate.crt")).status_code == 404

    async def test_upload_rejects_a_mismatched_pair(self, signed_in):
        cert_pem, _ = tls.generate_self_signed(hosts=["a.local"])
        _, other_key = tls.generate_self_signed(hosts=["b.local"])
        response = await signed_in.post(
            "/api/security/certificate/upload",
            json={"cert_pem": cert_pem, "key_pem": other_key},
        )
        assert response.status_code == 400
        assert "does not match" in response.json()["detail"]

    async def test_upload_accepts_a_valid_pair(self, signed_in):
        cert_pem, key_pem = tls.generate_self_signed(hosts=["own.example"])
        response = await signed_in.post(
            "/api/security/certificate/upload",
            json={"cert_pem": cert_pem, "key_pem": key_pem},
        )
        assert response.status_code == 200
        assert response.json()["certificate"]["source"] == "uploaded"

    async def test_base_url_round_trip(self, signed_in):
        response = await signed_in.post(
            "/api/security/base-url", json={"public_base_url": "https://nas.local:8443/"}
        )
        assert response.json()["public_base_url"] == "https://nas.local:8443"
        body = (await signed_in.get("/api/security")).json()
        assert body["public_base_url"] == "https://nas.local:8443"

    async def test_base_url_must_carry_a_scheme(self, signed_in):
        response = await signed_in.post(
            "/api/security/base-url", json={"public_base_url": "nas.local"}
        )
        assert response.status_code == 400

    async def test_base_url_feeds_the_oauth_redirect_uris(self, signed_in):
        await signed_in.post(
            "/api/security/base-url", json={"public_base_url": "https://nas.local:8443"}
        )
        body = (await signed_in.get("/api/integrations")).json()
        assert (
            body["redirect_uris"]["etsy"]
            == "https://nas.local:8443/api/integrations/etsy/callback"
        )

    async def test_https_redirect_toggle_persists(self, signed_in):
        assert (
            await signed_in.post("/api/security/https-redirect", json={"enabled": True})
        ).json() == {"https_redirect": True}
        assert (await signed_in.get("/api/security")).json()["https_redirect"] is True

    async def test_intervals_no_longer_take_a_base_url(self, signed_in):
        # The address moved to the security step; intervals only set intervals.
        await signed_in.post(
            "/api/security/base-url", json={"public_base_url": "https://nas.local"}
        )
        await signed_in.post("/api/setup/intervals", json={"etsy_minutes": 7})
        body = (await signed_in.get("/api/settings")).json()
        assert body["poll_intervals"]["etsy_minutes"] == 7
        assert body["public_base_url"] == "https://nas.local"
