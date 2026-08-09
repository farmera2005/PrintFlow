"""Non-credential configuration, stored in the database (never in env vars)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppSetting

KEY_SETUP_COMPLETE = "setup_complete"
KEY_POLL_INTERVALS = "poll_intervals"
KEY_PUBLIC_BASE_URL = "public_base_url"
KEY_HTTPS_REDIRECT = "https_redirect"
KEY_MANUFACTURING = "manufacturing"

# How a made-items sheet posts to QuickBooks. `account_id` is the account the
# manufacturing cost comes out of, and is the one field with no sensible
# default: it depends on the operator's chart of accounts, so posting is blocked
# until they choose it.
DEFAULT_MANUFACTURING: dict[str, Any] = {
    "account_id": None,
    "account_name": None,
    "payment_type": "Cash",
    "vendor_id": None,
    "vendor_name": None,
    "doc_number_prefix": "",
}

MANUFACTURING_FIELDS = tuple(DEFAULT_MANUFACTURING)
# QuickBooks rejects anything else on a Purchase.
PAYMENT_TYPES = ("Cash", "Check", "CreditCard")

# Defaults from §6 of the build spec.
DEFAULT_POLL_INTERVALS: dict[str, int] = {
    "etsy_minutes": 5,
    "bambuddy_minutes": 2,
    "shipstation_minutes": 10,
}

POLL_INTERVAL_BOUNDS: dict[str, tuple[int, int]] = {
    "etsy_minutes": (1, 240),
    "bambuddy_minutes": (1, 240),
    "shipstation_minutes": (1, 240),
}

_DEFAULTS: dict[str, Any] = {
    KEY_SETUP_COMPLETE: False,
    KEY_POLL_INTERVALS: DEFAULT_POLL_INTERVALS,
    KEY_PUBLIC_BASE_URL: None,
    # Off by default: a redirect that outlives a broken certificate would lock
    # the operator out of the only UI that can fix it.
    KEY_HTTPS_REDIRECT: False,
    KEY_MANUFACTURING: DEFAULT_MANUFACTURING,
}


async def get_setting(session: AsyncSession, key: str) -> Any:
    row = await session.get(AppSetting, key)
    if row is None:
        return _DEFAULTS.get(key)
    # Values are stored wrapped so that scalars (bool/str) round-trip through JSON.
    return row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value={"v": value}))
    else:
        row.value = {"v": value}
    await session.flush()


async def get_poll_intervals(session: AsyncSession) -> dict[str, int]:
    stored = await get_setting(session, KEY_POLL_INTERVALS) or {}
    merged = dict(DEFAULT_POLL_INTERVALS)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in merged and isinstance(value, int):
                merged[key] = value
    return merged


def clamp_poll_intervals(values: dict[str, Any]) -> dict[str, int]:
    out = dict(DEFAULT_POLL_INTERVALS)
    for key, (low, high) in POLL_INTERVAL_BOUNDS.items():
        raw = values.get(key)
        if raw is None:
            continue
        try:
            out[key] = max(low, min(high, int(raw)))
        except (TypeError, ValueError):
            continue
    return out


async def get_manufacturing_settings(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, KEY_MANUFACTURING) or {}
    merged = dict(DEFAULT_MANUFACTURING)
    if isinstance(stored, dict):
        for key in MANUFACTURING_FIELDS:
            if key in stored:
                merged[key] = stored[key]
    if merged.get("payment_type") not in PAYMENT_TYPES:
        merged["payment_type"] = DEFAULT_MANUFACTURING["payment_type"]
    return merged


async def set_manufacturing_settings(
    session: AsyncSession, values: dict[str, Any]
) -> dict[str, Any]:
    current = await get_manufacturing_settings(session)
    for key in MANUFACTURING_FIELDS:
        if key in values:
            current[key] = values[key]
    if current.get("payment_type") not in PAYMENT_TYPES:
        current["payment_type"] = DEFAULT_MANUFACTURING["payment_type"]
    await set_setting(session, KEY_MANUFACTURING, current)
    return current


async def is_setup_complete(session: AsyncSession) -> bool:
    return bool(await get_setting(session, KEY_SETUP_COMPLETE))


async def has_admin_user(session: AsyncSession) -> bool:
    from ..models import User

    return (await session.execute(select(User.id).limit(1))).first() is not None
