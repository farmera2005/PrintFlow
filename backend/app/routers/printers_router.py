"""Printers — the farm as Bambuddy reports it, with PrintFlow's plates on it."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
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


# A frame is tens of kilobytes; anything past this is not a picture, and
# holding it in memory to find out would be the fault rather than the check.
MAX_FRAME_BYTES = 8 * 1024 * 1024


async def _still_frame(response) -> tuple[bytes, str]:
    """One picture, whether the camera sends pictures or a film of them.

    Every build's stream is JPEG frames, so a frame is the bytes between the
    start and end markers — which needs no agreement about how the parts are
    separated, and no build has ever disagreed about those two bytes.
    """
    kind = str(response.headers.get("content-type") or "").split(";")[0].strip()
    if not kind.startswith("multipart/"):
        body = b""
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > MAX_FRAME_BYTES:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "The camera sent more than one frame's worth of data.",
                )
        return body, kind or "image/jpeg"

    buffer = b""
    async for chunk in response.aiter_bytes():
        buffer += chunk
        start = buffer.find(b"\xff\xd8")
        if start >= 0:
            end = buffer.find(b"\xff\xd9", start + 2)
            if end >= 0:
                return buffer[start : end + 2], "image/jpeg"
        if len(buffer) > MAX_FRAME_BYTES:
            break
    raise HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        "The camera is streaming something PrintFlow could not read a frame out of.",
    )


@router.get("/{printer_id}/camera")
async def printer_camera(
    printer_id: str,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
):
    """One picture from this machine's camera, proxied.

    Proxied rather than linked because neither thing the browser would need is
    true of it: the API key lives here, and Bambuddy is on the shop LAN while
    the person looking at PrintFlow may be on a phone through a tunnel.

    A picture rather than a stream, deliberately. Passing a live multipart
    stream through would hold one socket per card open for as long as the tab
    exists — ten of them, through a tunnel, on a page people leave up all day —
    and it would only work on the builds whose camera is multipart in the first
    place. Asking for a frame works on both kinds, and the page asks again as
    often as it wants a new one.
    """
    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    try:
        async with client.camera(
            printer_id, override=farm.camera_url_for(printer_id)
        ) as response:
            frame, kind = await _still_frame(response)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    # The page asks again for every new frame, so this must not come back out
    # of a cache: a frozen picture of a working printer is worse than none.
    return Response(frame, media_type=kind, headers={"Cache-Control": "no-store"})


@router.get("/cameras")
async def camera_check(
    again: bool = False,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Why the cards have no pictures on them, according to the instance.

    `again` forgets a stored "this build has no camera" and looks once more —
    the answer to press after naming a path under Advanced, or after upgrading
    the Bambuddy that did not have cameras last time anybody asked.
    """
    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Looking for cameras"):
            report = await bambuddy_api.camera_report(session, client, again=again)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await session.commit()
    return report


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
