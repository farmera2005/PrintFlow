"""Printers — the farm as Bambuddy reports it, with PrintFlow's plates on it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..integrations import base as base_api
from ..integrations import bambuddy as bambuddy_api
from ..integrations.base import IntegrationError
from ..models import PROVIDER_BAMBUDDY, User
from ..services import audit, controls, farm, printing
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
        async with bambuddy_api.open_camera(
            session, client, printer_id, override=farm.camera_url_for(printer_id)
        ) as response:
            frame, kind = await _still_frame(response)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    # Getting the picture may have worked out how this build wants its camera
    # token presented, which is worth exactly one round of trial and error.
    await session.commit()
    # The page asks again for every new frame, so this must not come back out
    # of a cache: a frozen picture of a working printer is worse than none.
    return Response(frame, media_type=kind, headers={"Cache-Control": "no-store"})


class PrintRequest(BaseModel):
    """One file, on one machine, right now."""

    bambuddy_archive_id: int | None = None
    bambuddy_file_path: str | None = None
    name: str | None = None
    plate_number: int = Field(default=1, ge=1)
    copies: int = Field(default=1, ge=1, le=50)
    print_options: dict[str, Any] = Field(default_factory=dict)
    # One id per open dialog, sent again unchanged when the operator presses
    # Print a second time. See print_on for why that matters.
    request_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _names_a_file(self) -> PrintRequest:
        if self.bambuddy_archive_id is None and not (self.bambuddy_file_path or "").strip():
            raise ValueError("Pick a file: give an archive id or a file path.")
        return self


@router.post("/{printer_id}/print")
async def print_on(
    printer_id: int,
    body: PrintRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Send a file from Bambuddy straight to this machine.

    Nothing to do with an order: this is the reprint, the test piece, the one
    the shop needs and nobody bought. It goes on the machine that was clicked
    rather than through dispatch, because the point of asking here is that the
    operator has already decided which machine — and it is written to the audit
    log, because it puts filament through a printer.
    """
    # Pressing Print twice must not print twice.
    #
    # The dangerous case is not a double click, it is an answer that never
    # arrives: a proxy in front of PrintFlow times out, or a tunnel drops, and
    # the operator sees an error for a plate that is already on the machine.
    # There is no way to tell that apart from a request that never landed, so
    # the honest thing is to make the retry safe rather than to ask them to
    # guess. The dialog sends the same id both times; a request that has
    # already been done reports what it did rather than doing it again.
    if body.request_id:
        already = await audit.find(
            session, action="print_now", key="request_id", value=body.request_id
        )
        if already is not None:
            return {
                "printer_id": printer_id,
                "queued": already.detail.get("queue_ids") or [],
                "copies": already.detail.get("copies") or 0,
                # Said plainly, because "it worked" and "it had already worked"
                # are different things to see after an error.
                "already_done": True,
                "done_at": already.created_at,
            }

    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    queued: list[Any] = []
    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Queueing a print"):
            for _ in range(body.copies):
                item = await bambuddy_api.with_healing(
                    session,
                    client,
                    ("queue",),
                    lambda: client.enqueue(
                        archive_id=body.bambuddy_archive_id,
                        file_path=body.bambuddy_file_path,
                        plate_number=body.plate_number,
                        printer_id=printer_id,
                        print_options=body.print_options,
                    ),
                )
                queued.append(item.get("id"))
    except IntegrationError as exc:
        # Some may already be on the machine. Saying how many went is the
        # difference between "try again" and "try again and cancel three".
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"{exc} — {len(queued)} of {body.copies} had already been queued."
            if queued
            else str(exc),
        ) from exc

    await audit.record(
        session,
        entity_type="printer",
        entity_id=None,
        action="print_now",
        detail={
            "printer_id": printer_id,
            "archive_id": body.bambuddy_archive_id,
            "file_path": body.bambuddy_file_path,
            "name": body.name,
            "plate_number": body.plate_number,
            "copies": body.copies,
            "queue_ids": queued,
            "request_id": body.request_id,
        },
        actor=user.username,
    )
    await session.commit()
    return {"printer_id": printer_id, "queued": queued, "copies": len(queued)}


@router.delete("/{printer_id}/queue/{queue_id}")
async def cancel_queued(
    printer_id: int,
    queue_id: int,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take one job out of a machine's queue.

    Bambuddy's queue, not PrintFlow's list — so this cancels whatever is
    sitting there, including something sent from Bambuddy's own screen. Where
    the item *is* one of PrintFlow's plates, the plate is cancelled with it:
    leaving a plate marked queued for a job that is no longer in any queue is
    the kind of disagreement nobody ever notices until dispatch tries again.
    """
    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Cancelling a queued job"):
            await bambuddy_api.with_healing(
                session, client, ("queue",), lambda: client.cancel(queue_id)
            )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    plate = await printing.cancel_by_queue_id(session, queue_id)
    await audit.record(
        session,
        entity_type="printer",
        entity_id=None,
        action="queue_cancel",
        detail={
            "printer_id": printer_id,
            "queue_id": queue_id,
            "print_job_id": str(plate.id) if plate else None,
        },
        actor=user.username,
    )
    await session.commit()
    return {"queue_id": queue_id, "print_job_cancelled": plate is not None}


@router.post("/{printer_id}/control/{action}")
async def run_control(
    printer_id: int,
    action: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Pause, resume, stop — or whatever else this Bambuddy offers.

    The page already knows which buttons apply, and draws the rest disabled.
    This checks again anyway, against a status read now rather than whenever
    that page last refreshed: the farm screen polls, and a print that finished
    thirty seconds ago still shows a live Pause. Sending it would at best do
    nothing and at worst pause the next plate, which is a print nobody is
    watching quietly stopping overnight.
    """
    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    try:
        offered = await bambuddy_api.ensure_controls(session, client)
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    if action not in offered:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This Bambuddy does not offer a {controls.label_for(action).lower()} "
            "control for a printer. PrintFlow reads what it can do from the "
            "instance's own API document — if the control exists under a name "
            "PrintFlow did not recognise, set it under Settings → Bambuddy → "
            "Advanced.",
        )

    # What the machine is doing now, not what the card said.
    async with base_api.deadline(PROVIDER_BAMBUDDY, "Reading the printer"):
        state = await farm.state_of(session, client, printer_id)
    if not controls.allowed(action, state):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{controls.label_for(action)} does not apply to this printer: "
            f"it is {state}.",
        )

    try:
        async with base_api.deadline(PROVIDER_BAMBUDDY, "Sending a printer control"):
            await bambuddy_api.with_healing(
                session,
                client,
                (bambuddy_api.control_role(action),),
                lambda: client.run_control(printer_id, action),
            )
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    # Anything that changes what a machine is physically doing is written down.
    await audit.record(
        session,
        entity_type="printer",
        entity_id=None,
        action="printer_control",
        detail={"printer_id": printer_id, "control": action, "state": state},
        actor=user.username,
    )
    await session.commit()
    return {"printer_id": printer_id, "control": action, "state": state, "sent": True}


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
