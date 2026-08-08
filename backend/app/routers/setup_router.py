"""First-run setup wizard. All configuration happens here — no config files (§2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import hash_password, issue_session, require_user
from ..db import get_session
from ..models import PROVIDERS, User
from ..services import credentials
from ..services.settings_store import (
    KEY_POLL_INTERVALS,
    KEY_PUBLIC_BASE_URL,
    KEY_SETUP_COMPLETE,
    clamp_poll_intervals,
    get_poll_intervals,
    get_setting,
    has_admin_user,
    is_setup_complete,
    set_setting,
)

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
async def setup_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Drives the wizard: which steps are done, which is next."""
    statuses = {row["provider"]: row for row in await credentials.status_summary(session)}
    admin_exists = await has_admin_user(session)
    steps = [
        {"key": "admin", "label": "Admin account", "complete": admin_exists},
        *[
            {
                "key": provider,
                "label": {
                    "etsy": "Etsy",
                    "qbo": "QuickBooks Online",
                    "bambuddy": "Bambuddy",
                    "shipstation": "ShipStation",
                }[provider],
                "complete": statuses[provider]["connected"],
            }
            for provider in PROVIDERS
        ],
        {"key": "intervals", "label": "Poll intervals", "complete": True},
    ]
    return {
        "setup_complete": await is_setup_complete(session),
        "admin_exists": admin_exists,
        "steps": steps,
        "integrations": list(statuses.values()),
        "poll_intervals": await get_poll_intervals(session),
        "public_base_url": await get_setting(session, KEY_PUBLIC_BASE_URL),
    }


class CreateAdminRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=8, max_length=72)


@router.post("/admin")
async def create_admin(
    body: CreateAdminRequest,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Step 1. Only callable while no admin exists — this endpoint is unauthenticated."""
    if await has_admin_user(session):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An admin account already exists. Sign in instead.",
        )
    try:
        password_hash = hash_password(body.password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    user = User(username=body.username.strip(), password_hash=password_hash)
    session.add(user)
    await session.commit()
    issue_session(response, user)
    return {"username": user.username}


class IntervalsRequest(BaseModel):
    etsy_minutes: int | None = None
    bambuddy_minutes: int | None = None
    shipstation_minutes: int | None = None
    public_base_url: str | None = None


@router.post("/intervals")
async def save_intervals(
    body: IntervalsRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    intervals = clamp_poll_intervals(body.model_dump(exclude_none=True))
    await set_setting(session, KEY_POLL_INTERVALS, intervals)
    if body.public_base_url is not None:
        await set_setting(
            session, KEY_PUBLIC_BASE_URL, body.public_base_url.strip().rstrip("/") or None
        )
    await session.commit()

    from ..scheduler import reschedule

    await reschedule()
    return {"poll_intervals": intervals}


@router.post("/complete")
async def complete_setup(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    await set_setting(session, KEY_SETUP_COMPLETE, True)
    await session.commit()

    from ..scheduler import reschedule

    await reschedule()
    return {"setup_complete": True}


@router.post("/reopen")
async def reopen_setup(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Re-enter the wizard from Settings without losing anything already connected."""
    await set_setting(session, KEY_SETUP_COMPLETE, False)
    await session.commit()
    return {"setup_complete": False}
