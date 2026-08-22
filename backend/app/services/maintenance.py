"""The maintenance book: what has been done to each machine, and when.

**Kept apart from the farm on purpose.** Everything else about a printer in
PrintFlow comes from Bambuddy and is only as durable as that connection: read
live, cached nowhere, gone the moment the instance is re-pointed or the printer
is re-added under a new id. A maintenance history cannot work that way. It is
the one record about a machine that has to survive changing farm managers,
re-imaging the box, and the machine itself leaving the farm — and shops run
printers Bambuddy never saw at all.

So these rows are PrintFlow's own. `bambuddy_printer_id` is a convenience that
lets the tab show what a machine is doing right now and lets a farm listing be
adopted in one press; nothing here depends on it being set, correct, or still
pointing at anything.

**The newest entry is the machine's condition.** Rather than a status column on
the machine that somebody has to remember to change, the condition is read from
the most recent log — which is a thing they were going to write anyway. One
place to look, and it cannot drift from the history that explains it.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    MAINT_ATTENTION,
    MAINT_DOWN,
    MAINT_DUE,
    MAINT_OK,
    MAINT_SERVICED,
    MAINTENANCE_STATUSES,
    Machine,
    MaintenanceLog,
)


class MaintenanceError(RuntimeError):
    """This cannot be recorded as it stands. The message is shown verbatim."""


# What each status is called on screen, and whether it is the sort of thing
# somebody should walk over and look at. Kept here rather than in the page so
# the two cannot disagree about what "due" means.
STATUS_LABELS: dict[str, str] = {
    MAINT_SERVICED: "Serviced",
    MAINT_OK: "Running fine",
    MAINT_DUE: "Service due",
    MAINT_ATTENTION: "Needs attention",
    MAINT_DOWN: "Out of service",
}

# The ones that want a person. A machine whose newest entry is one of these is
# sorted to the top of the tab, because a list in date order buries exactly the
# machine somebody opened the tab to find.
NEEDS_SOMEBODY = (MAINT_DOWN, MAINT_ATTENTION, MAINT_DUE)


def hours(raw: Any) -> Decimal | None:
    """A machine's hour counter, or nothing.

    Blank is a real answer: plenty of entries are written from a photograph of
    a nozzle rather than from the screen, and an invented zero would put a fake
    reading between two real ones. Rubbish is refused rather than read as zero
    for the same reason.
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MaintenanceError(f"'{raw}' is not a number of hours.") from exc
    if value < 0:
        raise MaintenanceError("Hours cannot be negative.")
    return value.quantize(Decimal("0.01"))


def check_status(raw: Any) -> str:
    value = str(raw or "").strip().lower()
    if value not in MAINTENANCE_STATUSES:
        raise MaintenanceError(
            f"'{raw}' is not a status. Use one of: "
            + ", ".join(f"{name} ({STATUS_LABELS[name]})" for name in MAINTENANCE_STATUSES)
            + "."
        )
    return value


def check_date(raw: Any) -> date:
    """When it happened. Today when nobody said, because that is the usual case."""
    if raw is None or str(raw).strip() == "":
        return datetime.now(timezone.utc).date()
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    try:
        return date.fromisoformat(str(raw).strip()[:10])
    except ValueError as exc:
        raise MaintenanceError(f"'{raw}' is not a date.") from exc


def serialize_log(log: MaintenanceLog) -> dict[str, Any]:
    return {
        "id": str(log.id),
        "machine_id": str(log.machine_id),
        "logged_on": log.logged_on.isoformat(),
        "hours": str(log.hours) if log.hours is not None else None,
        "status": log.status,
        "status_label": STATUS_LABELS.get(log.status, log.status),
        "notes": log.notes,
        "actor": log.actor,
        "created_at": log.created_at,
    }


def serialize(machine: Machine) -> dict[str, Any]:
    """A machine with its book, and what the top of that book says about it."""
    logs = list(machine.logs)
    newest = logs[0] if logs else None
    # The last reading anybody wrote down, which is not always the newest entry
    # — a note about a rattle need not carry the hour counter.
    last_hours = next((row.hours for row in logs if row.hours is not None), None)
    return {
        "id": str(machine.id),
        "name": machine.name,
        "model": machine.model,
        "serial": machine.serial,
        "bambuddy_printer_id": machine.bambuddy_printer_id,
        "notes": machine.notes,
        "active": machine.active,
        # Read from the newest entry rather than stored, so it cannot drift from
        # the history underneath it. Null for a machine nobody has logged yet,
        # which is a real state and not an error.
        "status": newest.status if newest else None,
        "status_label": STATUS_LABELS.get(newest.status) if newest else None,
        "needs_somebody": bool(newest and newest.status in NEEDS_SOMEBODY),
        "last_logged_on": newest.logged_on.isoformat() if newest else None,
        "last_hours": str(last_hours) if last_hours is not None else None,
        "logs": [serialize_log(row) for row in logs],
        "created_at": machine.created_at,
    }


def _rank(row: dict[str, Any]) -> tuple[Any, ...]:
    """Trouble first, then never-logged, then alphabetical.

    A maintenance tab sorted by name buries the machine somebody opened it to
    find. A machine with no entries at all sits just under the broken ones: it
    is not a problem, but it is the other thing worth noticing.
    """
    return (
        not row["needs_somebody"],
        row["status"] is not None,
        row["name"].lower(),
    )


async def machines(session: AsyncSession, *, include_retired: bool = True) -> list[dict[str, Any]]:
    query = select(Machine).options(selectinload(Machine.logs))
    if not include_retired:
        query = query.where(Machine.active.is_(True))
    rows = (await session.execute(query)).scalars().all()
    listed = [serialize(machine) for machine in rows]
    listed.sort(key=_rank)
    # Retired machines keep their history but stop competing for attention.
    listed.sort(key=lambda row: not row["active"])
    return listed


async def get_machine(session: AsyncSession, machine_id: uuid.UUID) -> Machine | None:
    return (
        await session.execute(
            select(Machine)
            .where(Machine.id == machine_id)
            .options(selectinload(Machine.logs))
        )
    ).scalars().one_or_none()


async def reloaded(session: AsyncSession, machine: Machine) -> Machine:
    """The machine with its logs re-read, after one has been added or removed.

    A collection already loaded is not re-read when rows appear underneath it,
    so the answer sent back would otherwise be the one from before the write.
    """
    return (
        await session.execute(
            select(Machine)
            .where(Machine.id == machine.id)
            .options(selectinload(Machine.logs))
            .execution_options(populate_existing=True)
        )
    ).scalars().one()


async def add_machine(
    session: AsyncSession,
    *,
    name: str,
    model: str | None = None,
    serial: str | None = None,
    bambuddy_printer_id: str | None = None,
    notes: str | None = None,
) -> Machine:
    label = (name or "").strip()
    if not label:
        raise MaintenanceError("A machine needs a name.")
    existing = (
        await session.execute(select(Machine).where(Machine.name == label))
    ).scalars().one_or_none()
    if existing is not None:
        raise MaintenanceError(f"There is already a machine called {label}.")
    machine = Machine(
        name=label,
        model=(model or "").strip() or None,
        serial=(serial or "").strip() or None,
        bambuddy_printer_id=(str(bambuddy_printer_id).strip() or None)
        if bambuddy_printer_id is not None
        else None,
        notes=(notes or "").strip() or None,
    )
    session.add(machine)
    await session.flush()
    return machine


async def add_log(
    session: AsyncSession,
    machine: Machine,
    *,
    logged_on: Any = None,
    hours_raw: Any = None,
    status: Any = MAINT_SERVICED,
    notes: str | None = None,
    actor: str | None = None,
) -> MaintenanceLog:
    text = (notes or "").strip() or None
    reading = hours(hours_raw)
    kind = check_status(status)
    if text is None and reading is None:
        # A date and a status on their own say nothing anybody can act on later.
        raise MaintenanceError("Write a note or an hour reading — ideally both.")
    entry = MaintenanceLog(
        machine_id=machine.id,
        logged_on=check_date(logged_on),
        hours=reading,
        status=kind,
        notes=text,
        actor=actor,
    )
    session.add(entry)
    await session.flush()
    return entry


async def adoptable(session: AsyncSession, printers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Farm printers that have no machine here yet.

    Offered rather than created: a shop's maintenance book is its own, and
    filling it automatically from whatever a farm manager happens to be
    reporting today is how it fills with machines nobody services.
    """
    known = {
        str(row)
        for row in (
            await session.execute(
                select(Machine.bambuddy_printer_id).where(
                    Machine.bambuddy_printer_id.is_not(None)
                )
            )
        ).scalars().all()
    }
    names = {
        name.strip().lower()
        for name in (await session.execute(select(Machine.name))).scalars().all()
    }
    out: list[dict[str, Any]] = []
    for printer in printers:
        printer_id = printer.get("id")
        name = str(printer.get("name") or "").strip()
        if printer_id is None or str(printer_id) in known:
            continue
        if name and name.lower() in names:
            continue
        out.append(
            {
                "bambuddy_printer_id": str(printer_id),
                "name": name or f"Printer {printer_id}",
                "model": printer.get("model"),
                "serial": printer.get("serial"),
            }
        )
    return out
