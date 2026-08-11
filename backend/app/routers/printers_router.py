"""Printers — the farm as Bambuddy reports it, with PrintFlow's plates on it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..integrations import base as base_api
from ..integrations import bambuddy as bambuddy_api
from ..integrations.base import IntegrationError
from ..models import PROVIDER_BAMBUDDY, User
from ..services import farm
from ..services.credentials import IntegrationNotConfigured

router = APIRouter(prefix="/api/printers", tags=["printers"])


@router.get("")
async def list_printers(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    async with base_api.deadline(PROVIDER_BAMBUDDY, "Reading the farm"):
        overview = await farm.overview(session)
    # read_farm may have adopted a corrected endpoint on the way.
    await session.commit()
    return overview


@router.get("/raw")
async def printers_raw(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """What Bambuddy actually replied, for cards that came back blank.

    Same reason as the file picker's version: no two self-hosted builds spell a
    temperature the same, and a card of empty readings cannot be diagnosed from
    outside. It can be shown.
    """
    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Reading the farm"):
            return await client.raw_farm()
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
