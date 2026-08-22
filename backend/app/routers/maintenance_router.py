"""Maintenance — PrintFlow's own book of what has been done to each machine.

Nothing here talks to QuickBooks or Etsy, and the only thing it asks Bambuddy is
"what printers are there", once, so a machine can be adopted rather than typed.
The records are PrintFlow's own and outlive every one of those connections.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..integrations import bambuddy as bambuddy_api
from ..integrations.base import IntegrationError
from ..models import MAINTENANCE_STATUSES, MaintenanceLog, User
from ..services import audit, maintenance
from ..services.credentials import IntegrationNotConfigured
from ..services.maintenance import MaintenanceError

router = APIRouter(prefix="/api/maintenance", tags=["maintenance"])


def _fail(exc: MaintenanceError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


async def _machine(session: AsyncSession, machine_id: uuid.UUID):
    machine = await maintenance.get_machine(session, machine_id)
    if machine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such machine.")
    return machine


@router.get("")
async def read_all(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Every machine with its book, worst first."""
    return {
        "machines": await maintenance.machines(session),
        # So the page can label a select without hardcoding the vocabulary.
        "statuses": [
            {"value": name, "label": maintenance.STATUS_LABELS[name]}
            for name in MAINTENANCE_STATUSES
        ],
    }


@router.get("/adoptable")
async def read_adoptable(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Printers on the farm that have no machine here yet.

    A convenience, and only a convenience: a farm that cannot be reached is an
    empty list rather than an error, because the maintenance book does not
    depend on Bambuddy for anything.
    """
    try:
        client = await bambuddy_api.client_for(session)
        printers = await bambuddy_api.with_healing(
            session, client, ("printers",), client.list_printers
        )
    except (IntegrationError, IntegrationNotConfigured) as exc:
        return {"printers": [], "why": str(exc)}
    return {"printers": await maintenance.adoptable(session, printers), "why": None}


class MachineRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    serial: str | None = Field(default=None, max_length=200)
    bambuddy_printer_id: str | None = Field(default=None, max_length=100)
    notes: str | None = None


@router.post("/machines")
async def add_machine(
    body: MachineRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        machine = await maintenance.add_machine(
            session,
            name=body.name,
            model=body.model,
            serial=body.serial,
            bambuddy_printer_id=body.bambuddy_printer_id,
            notes=body.notes,
        )
    except MaintenanceError as exc:
        raise _fail(exc) from exc
    await audit.record(
        session,
        entity_type="machine",
        entity_id=machine.id,
        action="machine_added",
        detail={"name": machine.name, "model": machine.model},
        actor=user.username,
    )
    await session.commit()
    return maintenance.serialize(await maintenance.reloaded(session, machine))


class MachineUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    serial: str | None = Field(default=None, max_length=200)
    bambuddy_printer_id: str | None = Field(default=None, max_length=100)
    notes: str | None = None
    active: bool | None = None


@router.patch("/machines/{machine_id}")
async def edit_machine(
    machine_id: uuid.UUID,
    body: MachineUpdate,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    machine = await _machine(session, machine_id)
    values = body.model_dump(exclude_unset=True)
    if "name" in values:
        name = (values["name"] or "").strip()
        if not name:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "A machine needs a name.")
        machine.name = name
    for field in ("model", "serial", "bambuddy_printer_id", "notes"):
        if field in values:
            setattr(machine, field, (values[field] or "").strip() or None)
    if "active" in values:
        machine.active = bool(values["active"])
    await session.flush()
    await audit.record(
        session,
        entity_type="machine",
        entity_id=machine.id,
        action="machine_changed",
        detail={"name": machine.name, "active": machine.active},
        actor=user.username,
    )
    await session.commit()
    return maintenance.serialize(await maintenance.reloaded(session, machine))


@router.delete("/machines/{machine_id}")
async def remove_machine(
    machine_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete a machine and its whole book.

    Offered because a machine typed in by mistake should not be permanent, but
    it is the destructive one: retiring a machine keeps the history and takes
    it out of the way, which is what is wanted nearly every time.
    """
    machine = await _machine(session, machine_id)
    name, count = machine.name, len(machine.logs)
    await session.delete(machine)
    await audit.record(
        session,
        entity_type="machine",
        entity_id=machine_id,
        action="machine_deleted",
        detail={"name": name, "logs": count},
        actor=user.username,
    )
    await session.commit()
    return {"deleted": True, "name": name, "logs": count}


class LogRequest(BaseModel):
    """One entry: hours, date, notes, status."""

    logged_on: str | None = None
    hours: str | float | int | None = None
    status: str = "serviced"
    notes: str | None = None


@router.post("/machines/{machine_id}/logs")
async def add_log(
    machine_id: uuid.UUID,
    body: LogRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    machine = await _machine(session, machine_id)
    try:
        entry = await maintenance.add_log(
            session,
            machine,
            logged_on=body.logged_on,
            hours_raw=body.hours,
            status=body.status,
            notes=body.notes,
            actor=user.username,
        )
    except MaintenanceError as exc:
        raise _fail(exc) from exc
    await audit.record(
        session,
        entity_type="machine",
        entity_id=machine.id,
        action="maintenance_logged",
        detail={
            "machine": machine.name,
            "logged_on": entry.logged_on.isoformat(),
            "hours": str(entry.hours) if entry.hours is not None else None,
            "status": entry.status,
        },
        actor=user.username,
    )
    await session.commit()
    return maintenance.serialize(await maintenance.reloaded(session, machine))


@router.patch("/machines/{machine_id}/logs/{log_id}")
async def edit_log(
    machine_id: uuid.UUID,
    log_id: uuid.UUID,
    body: LogRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Correct an entry.

    Editable rather than immutable, unlike anything that reaches an accounting
    system: this is a shop's own notebook, and a typo in an hour reading should
    be crossed out rather than lived with. The change is written to the audit
    log, which is where the crossing-out is kept.
    """
    machine = await _machine(session, machine_id)
    entry = await session.get(MaintenanceLog, log_id)
    if entry is None or entry.machine_id != machine.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such entry on this machine.")
    values = body.model_dump(exclude_unset=True)
    before: dict[str, Any] = {
        "logged_on": entry.logged_on.isoformat(),
        "hours": str(entry.hours) if entry.hours is not None else None,
        "status": entry.status,
    }
    try:
        if "logged_on" in values:
            entry.logged_on = maintenance.check_date(values["logged_on"])
        if "hours" in values:
            entry.hours = maintenance.hours(values["hours"])
        if "status" in values:
            entry.status = maintenance.check_status(values["status"])
        if "notes" in values:
            entry.notes = (values["notes"] or "").strip() or None
        if entry.notes is None and entry.hours is None:
            raise MaintenanceError("Write a note or an hour reading — ideally both.")
    except MaintenanceError as exc:
        raise _fail(exc) from exc
    await session.flush()
    await audit.record(
        session,
        entity_type="machine",
        entity_id=machine.id,
        action="maintenance_log_changed",
        detail={"machine": machine.name, "log_id": str(log_id), "was": before},
        actor=user.username,
    )
    await session.commit()
    return maintenance.serialize(await maintenance.reloaded(session, machine))


@router.delete("/machines/{machine_id}/logs/{log_id}")
async def remove_log(
    machine_id: uuid.UUID,
    log_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    machine = await _machine(session, machine_id)
    entry = await session.get(MaintenanceLog, log_id)
    if entry is None or entry.machine_id != machine.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such entry on this machine.")
    removed = {
        "logged_on": entry.logged_on.isoformat(),
        "hours": str(entry.hours) if entry.hours is not None else None,
        "status": entry.status,
        "notes": entry.notes,
    }
    await session.delete(entry)
    await audit.record(
        session,
        entity_type="machine",
        entity_id=machine.id,
        action="maintenance_log_deleted",
        # The whole entry, because deleting it is the only way it stops being
        # readable anywhere else.
        detail={"machine": machine.name, "entry": removed},
        actor=user.username,
    )
    await session.commit()
    return maintenance.serialize(await maintenance.reloaded(session, machine))
