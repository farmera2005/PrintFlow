"""HTTPS certificate generation, validation and storage.

Etsy and Intuit both want an https redirect URI, so PrintFlow needs a working
certificate before the OAuth steps of the wizard can be completed. Asking an
operator to run `openssl` on their NAS is a bad first experience, so the app
generates a self-signed certificate itself: automatically on first boot (so
HTTPS is live immediately) and again from the setup wizard once the operator
has told it which hostnames and IPs it will actually be reached on.

Self-signed is the right default here — this is a LAN service with no public
DNS name. The browser warning is expected; the wizard offers the certificate
for download so it can be added to a trust store. An operator with a real
certificate can upload one instead.
"""

from __future__ import annotations

import datetime
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import decrypt_json, encrypt_json
from ..models import TlsCertificate

log = logging.getLogger("printflow.tls")

# ~27 months: the longest a self-signed leaf can be while still being accepted
# by browsers that enforce a maximum lifetime.
DEFAULT_VALIDITY_DAYS = 825
MAX_VALIDITY_DAYS = 3650

CERT_FILENAME = "cert.pem"
KEY_FILENAME = "key.pem"


class CertificateError(ValueError):
    """A certificate or key could not be parsed, or the pair does not match."""


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------


def split_hosts(values: list[str]) -> tuple[list[str], list[str]]:
    """Sort user-entered names into (hostnames, ip addresses), de-duplicated."""
    hostnames: list[str] = []
    addresses: list[str] = []
    for raw in values:
        candidate = (raw or "").strip()
        if not candidate:
            continue
        # Tolerate someone pasting a URL or a host:port.
        for prefix in ("https://", "http://"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
        candidate = candidate.split("/")[0]
        if candidate.count(":") == 1:
            candidate = candidate.split(":")[0]
        if not candidate:
            continue
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            if candidate not in hostnames:
                hostnames.append(candidate)
        else:
            if candidate not in addresses:
                addresses.append(candidate)
    return hostnames, addresses


def local_hostnames() -> list[str]:
    """Best-effort guess at the names this host answers to, for pre-filling."""
    names: list[str] = ["localhost"]
    addresses: list[str] = ["127.0.0.1"]
    try:
        hostname = socket.gethostname()
        if hostname and hostname not in names:
            names.append(hostname)
        try:
            fqdn = socket.getfqdn()
            if fqdn and fqdn not in names:
                names.append(fqdn)
        except OSError:
            pass
        for info in socket.getaddrinfo(hostname, None):
            address = info[4][0]
            if address not in addresses and not address.startswith("fe80"):
                addresses.append(address)
    except OSError:
        pass
    return names + addresses


def generate_self_signed(
    *,
    hosts: list[str],
    validity_days: int = DEFAULT_VALIDITY_DAYS,
    common_name: str | None = None,
) -> tuple[str, str]:
    """Return (cert_pem, key_pem) for a self-signed server certificate."""
    hostnames, addresses = split_hosts(hosts)
    # localhost always works, whatever else the operator asked for.
    for fallback_host in ("localhost",):
        if fallback_host not in hostnames:
            hostnames.append(fallback_host)
    if "127.0.0.1" not in addresses:
        addresses.append("127.0.0.1")

    validity_days = max(1, min(int(validity_days), MAX_VALIDITY_DAYS))
    subject_cn = (common_name or hostnames[0] or "printflow")[:64]

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, subject_cn),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PrintFlow"),
        ]
    )
    alt_names: list[x509.GeneralName] = [x509.DNSName(host) for host in hostnames]
    alt_names += [x509.IPAddress(ipaddress.ip_address(a)) for a in addresses]

    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        # Backdated slightly so a clock skew between the NAS and the browser
        # does not make a freshly generated certificate "not yet valid".
        .not_valid_before(now - datetime.timedelta(minutes=10))
        .not_valid_after(now + datetime.timedelta(days=validity_days))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .sign(key, hashes.SHA256())
    )

    cert_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return cert_pem, key_pem


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------


def load_certificate(cert_pem: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(cert_pem.encode("utf-8"))
    except Exception as exc:  # cryptography raises a family of errors here
        raise CertificateError(
            "That does not look like a PEM certificate. It should start with "
            "'-----BEGIN CERTIFICATE-----'."
        ) from exc


def load_private_key(key_pem: str):
    try:
        return serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
    except TypeError as exc:
        raise CertificateError(
            "That private key is passphrase-protected. Remove the passphrase first."
        ) from exc
    except Exception as exc:
        raise CertificateError(
            "That does not look like a PEM private key. It should start with "
            "'-----BEGIN PRIVATE KEY-----'."
        ) from exc


def validate_pair(cert_pem: str, key_pem: str) -> None:
    """Reject a certificate and key that do not belong together."""
    certificate = load_certificate(cert_pem)
    key = load_private_key(key_pem)
    public_format = dict(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if key.public_key().public_bytes(**public_format) != certificate.public_key().public_bytes(
        **public_format
    ):
        raise CertificateError("That private key does not match that certificate.")


def _not_before(certificate: x509.Certificate) -> datetime.datetime:
    return getattr(certificate, "not_valid_before_utc", None) or certificate.not_valid_before


def _not_after(certificate: x509.Certificate) -> datetime.datetime:
    return getattr(certificate, "not_valid_after_utc", None) or certificate.not_valid_after


def describe(cert_pem: str) -> dict[str, Any]:
    certificate = load_certificate(cert_pem)
    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        names = [str(entry) for entry in san.get_values_for_type(x509.DNSName)]
        names += [str(entry) for entry in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []

    not_after = _not_after(certificate)
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=datetime.timezone.utc)
    remaining = not_after - datetime.datetime.now(datetime.timezone.utc)

    return {
        "common_name": _common_name(certificate.subject),
        "issuer": _common_name(certificate.issuer),
        "sans": names,
        "not_before": _not_before(certificate),
        "not_after": not_after,
        "days_remaining": max(0, remaining.days),
        "expired": remaining.total_seconds() <= 0,
        "self_signed": certificate.subject == certificate.issuer,
        "fingerprint_sha256": certificate.fingerprint(hashes.SHA256()).hex(":").upper(),
        "key_type": type(certificate.public_key()).__name__.replace("_", ""),
    }


def _common_name(name: x509.Name) -> str | None:
    values = name.get_attributes_for_oid(NameOID.COMMON_NAME)
    return values[0].value if values else None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


async def active_certificate(session: AsyncSession) -> TlsCertificate | None:
    return (
        await session.execute(
            select(TlsCertificate)
            .where(TlsCertificate.active.is_(True))
            .order_by(TlsCertificate.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def save(
    session: AsyncSession, cert_pem: str, key_pem: str, *, source: str = "generated"
) -> TlsCertificate:
    validate_pair(cert_pem, key_pem)
    details = describe(cert_pem)
    await session.execute(
        update(TlsCertificate).where(TlsCertificate.active.is_(True)).values(active=False)
    )
    row = TlsCertificate(
        cert_pem=cert_pem,
        encrypted_key=encrypt_json({"key_pem": key_pem}),
        fingerprint_sha256=details["fingerprint_sha256"],
        common_name=details["common_name"],
        sans=details["sans"],
        not_before=details["not_before"],
        not_after=details["not_after"],
        self_signed=details["self_signed"],
        source=source,
        active=True,
    )
    session.add(row)
    await session.flush()
    return row


def private_key_of(row: TlsCertificate) -> str:
    return decrypt_json(row.encrypted_key).get("key_pem", "")


async def ensure_certificate(
    session: AsyncSession, *, hosts: list[str] | None = None
) -> TlsCertificate:
    """Guarantee an active certificate exists — called on every boot."""
    existing = await active_certificate(session)
    if existing is not None:
        return existing
    cert_pem, key_pem = generate_self_signed(hosts=hosts or local_hostnames())
    log.info("Generated a self-signed HTTPS certificate for first boot")
    return await save(session, cert_pem, key_pem, source="generated")


def materialize(cert_pem: str, key_pem: str, directory: Path) -> tuple[Path, Path]:
    """Write the pair to disk for uvicorn, which wants file paths."""
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:  # pragma: no cover - depends on the host filesystem
        pass
    cert_path = directory / CERT_FILENAME
    key_path = directory / KEY_FILENAME
    cert_path.write_text(cert_pem)
    key_path.write_text(key_pem)
    cert_path.chmod(0o600)
    key_path.chmod(0o600)
    return cert_path, key_path
