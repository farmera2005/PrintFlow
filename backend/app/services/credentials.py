"""Read/write of encrypted integration credentials plus health bookkeeping."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..crypto import CredentialDecryptionError, decrypt_json, encrypt_json
from ..models import IntegrationCredential


class IntegrationNotConfigured(RuntimeError):
    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} is not connected yet. Connect it in Settings.")
        self.provider = provider


async def get_record(session: AsyncSession, provider: str) -> IntegrationCredential | None:
    return (
        await session.execute(
            select(IntegrationCredential).where(IntegrationCredential.provider == provider)
        )
    ).scalar_one_or_none()


async def load(session: AsyncSession, provider: str) -> dict[str, Any]:
    """Return the decrypted payload, or {} when the provider is not configured."""
    record = await get_record(session, provider)
    if record is None:
        return {}
    return decrypt_json(record.encrypted_payload)


async def require(session: AsyncSession, provider: str) -> dict[str, Any]:
    payload = await load(session, provider)
    if not payload:
        raise IntegrationNotConfigured(provider)
    return payload


async def save(
    session: AsyncSession,
    provider: str,
    payload: dict[str, Any],
    *,
    mark_connected: bool = True,
) -> IntegrationCredential:
    record = await get_record(session, provider)
    now = datetime.now(timezone.utc)
    if record is None:
        record = IntegrationCredential(provider=provider, encrypted_payload=b"")
        session.add(record)
    record.encrypted_payload = encrypt_json(payload)
    if mark_connected:
        record.connected_at = record.connected_at or now
        record.last_ok_at = now
        record.last_error = None
        record.last_error_at = None
    await session.flush()
    return record


async def merge(session: AsyncSession, provider: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Patch a subset of fields, leaving the rest of the payload untouched."""
    payload = await load(session, provider)
    payload.update(updates)
    await save(session, provider, payload, mark_connected=False)
    return payload


async def mark_ok(session: AsyncSession, provider: str) -> None:
    record = await get_record(session, provider)
    if record is None:
        return
    record.last_ok_at = datetime.now(timezone.utc)
    record.last_error = None
    record.last_error_at = None
    await session.flush()


async def mark_error(session: AsyncSession, provider: str, message: str) -> None:
    record = await get_record(session, provider)
    if record is None:
        return
    record.last_error = message[:2000]
    record.last_error_at = datetime.now(timezone.utc)
    await session.flush()


async def delete(session: AsyncSession, provider: str) -> None:
    record = await get_record(session, provider)
    if record is not None:
        await session.delete(record)
        await session.flush()


async def status_summary(session: AsyncSession) -> list[dict[str, Any]]:
    """Per-provider connection health, used for the Settings screen and banners."""
    from ..models import PROVIDERS

    records = {
        r.provider: r
        for r in (await session.execute(select(IntegrationCredential))).scalars().all()
    }
    out: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        record = records.get(provider)
        detail: dict[str, Any] = {}
        undecryptable = False
        if record is not None:
            try:
                payload = decrypt_json(record.encrypted_payload)
                detail = _public_detail(provider, payload)
            except CredentialDecryptionError:
                undecryptable = True
        out.append(
            {
                "provider": provider,
                "connected": record is not None and not undecryptable,
                "undecryptable": undecryptable,
                "connected_at": record.connected_at if record else None,
                "last_ok_at": record.last_ok_at if record else None,
                "last_error": record.last_error if record else None,
                "last_error_at": record.last_error_at if record else None,
                "detail": detail,
            }
        )
    return out


def _public_detail(provider: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Fields safe to show in the UI — never secrets or tokens."""
    if provider == "etsy":
        return {
            "shop_id": payload.get("shop_id"),
            "shop_name": payload.get("shop_name"),
            "keystring_hint": _hint(payload.get("keystring")),
        }
    if provider == "qbo":
        return {
            "realm_id": payload.get("realm_id"),
            "company_name": payload.get("company_name"),
            "environment": payload.get("environment", "production"),
            "client_id_hint": _hint(payload.get("client_id")),
        }
    if provider == "bambuddy":
        return {
            "base_url": payload.get("base_url"),
            "api_version": payload.get("api_version"),
            "printers": payload.get("printers", []),
        }
    if provider == "shipstation":
        return {
            "store_id": payload.get("store_id"),
            "store_name": payload.get("store_name"),
            "api_key_hint": _hint(payload.get("api_key")),
            # Whether delivery detection is switched on — not the key itself.
            # The board needs to be able to say why nothing ever reaches
            # Complete on its own, and "you have not pasted the tracking key"
            # is a far better answer than silence.
            "tracking": bool(payload.get("tracking_api_key")),
            "tracking_key_hint": _hint(payload.get("tracking_api_key")),
        }
    if provider == "wix":
        return {
            # The site id is not a secret — it is in the dashboard URL — and
            # showing it is how somebody confirms they connected the right one
            # of an account's sites.
            "site_id": payload.get("site_id"),
            "api_key_hint": _hint(payload.get("api_key")),
            "orders_since": payload.get("orders_since"),
        }
    return {}


def _hint(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return f"…{value[-4:]}" if len(value) > 4 else "…"
