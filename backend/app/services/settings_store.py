"""Non-credential configuration, stored in the database (never in env vars)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppSetting

KEY_SETUP_COMPLETE = "setup_complete"
KEY_POLL_INTERVALS = "poll_intervals"
KEY_PUBLIC_BASE_URL = "public_base_url"

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


async def is_setup_complete(session: AsyncSession) -> bool:
    return bool(await get_setting(session, KEY_SETUP_COMPLETE))


async def has_admin_user(session: AsyncSession) -> bool:
    from ..models import User

    return (await session.execute(select(User.id).limit(1))).first() is not None
