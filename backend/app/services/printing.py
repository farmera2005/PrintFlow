"""Print job planning, dispatch to Bambuddy, and status reconciliation (§4.3)."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..integrations import bambuddy as bambuddy_api
from ..integrations.base import IntegrationError
from ..models import (
    JOB_CANCELLED,
    LINE_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_PENDING,
    JOB_PRINTING,
    JOB_QUEUED,
    PROVIDER_BAMBUDDY,
    Order,
    OrderLine,
    PrintJob,
    PrintFile,
    ProductVariation,
)
from ..services import credentials, variations
from ..services.credentials import IntegrationNotConfigured
from ..services.state import recompute_order_by_id

log = logging.getLogger("printflow.printing")

# Jobs that still count toward covering a line's print quantity.
LIVE_JOB_STATUSES = (JOB_PENDING, JOB_QUEUED, JOB_PRINTING, JOB_DONE)


def plates_needed(qty_to_print: int, units_per_plate: int) -> int:
    """One Bambuddy queue item prints exactly one plate, so this is the job count."""
    if qty_to_print <= 0:
        return 0
    if units_per_plate <= 0:
        raise ValueError("units_per_plate must be positive")
    return math.ceil(qty_to_print / units_per_plate)


async def plan_jobs(session: AsyncSession, lines: list[OrderLine]) -> list[PrintJob]:
    """Create the pending print_jobs a line's shortfall needs. Idempotent."""
    targets = [line for line in lines if line.qty_to_print > 0 and line.product_id]
    if not targets:
        return []

    files: dict[Any, list[PrintFile]] = {}
    for row in (
        (
            await session.execute(
                select(PrintFile).where(
                    PrintFile.product_id.in_([line.product_id for line in targets])
                ).order_by(PrintFile.created_at, PrintFile.id)
            )
        )
        .scalars()
        .all()
    ):
        files.setdefault(row.product_id, []).append(row)

    # A variation can print a different file entirely — "with fan" and "without
    # fan" are not the same plate — so it gets the last word on what is queued.
    variation_rows = {
        variation.id: variation
        for variation in (
            await session.execute(
                select(ProductVariation).where(
                    ProductVariation.id.in_(
                        [line.variation_id for line in targets if line.variation_id]
                    )
                )
            )
        )
        .scalars()
        .all()
    }

    created: list[PrintJob] = []
    for line in targets:
        variation = variation_rows.get(line.variation_id)
        plans = variations.print_plans(files.get(line.product_id, []), variation)
        if not plans:
            line.stock_note = (
                (line.stock_note + " ") if line.stock_note else ""
            ) + "No Bambuddy print file for this product — cannot queue prints."
            continue

        candidates = [plan.as_candidate() for plan in plans]
        # Every plate has to cover the line whichever file ends up printing it,
        # so the yield is the least any of them promises. Over-printing wastes
        # filament; under-printing sends an order out short.
        units = min(plan.units_per_plate for plan in plans)

        existing = list(
            (
                await session.execute(
                    select(PrintJob).where(
                        PrintJob.order_line_id == line.id,
                        PrintJob.status.in_(LIVE_JOB_STATUSES),
                    )
                )
            )
            .scalars()
            .all()
        )

        # A variation set up after the order arrived changes what should be
        # printed, and so does adding or removing one of the product's files. A
        # job that has not reached Bambuddy is still only an intention, so
        # correct it; anything queued or printing is a fact on a machine and
        # hiding it would not unprint it.
        stale = [
            job
            for job in existing
            if job.status == JOB_PENDING and (job.candidates or []) != candidates
        ]
        for job in stale:
            await session.delete(job)
        if stale:
            existing = [job for job in existing if job not in stale]
            await session.flush()

        required = plates_needed(line.qty_to_print, units)
        for _ in range(max(0, required - len(existing))):
            job = PrintJob(
                order_line_id=line.id,
                candidates=candidates,
                # Seeded from the file this would use if it went out now, so a
                # plate on the queue screen names something before it is sent.
                # Dispatch overwrites these if it picks a different one.
                bambuddy_archive_id=plans[0].bambuddy_archive_id,
                bambuddy_file_path=plans[0].bambuddy_file_path,
                plate_number=plans[0].plate_number,
                printer_models=list(plans[0].printer_models),
                printer_id=plans[0].printer_id,
                units_expected=units,
                status=JOB_PENDING,
            )
            session.add(job)
            created.append(job)

    await session.flush()
    return created


def choose_printer(
    models: list[str],
    printers: list[dict],
    placed: dict[int, int] | None = None,
) -> int | None:
    """A printer of one of these models, or None if the farm has none free.

    Online and idle first, then online, then anything of the right model — a
    machine that is merely busy will get to it, whereas one that is offline
    will not, and a job placed on an unreachable printer just sits there.

    `placed` counts what this dispatch pass has already sent to each printer, so
    ten plates of the same part spread across the machines that can make them
    instead of stacking up behind one. Ties break on printer id, which keeps the
    same farm and the same queue producing the same answer twice running.
    """
    wanted = {str(model).strip().casefold() for model in models if str(model).strip()}
    if not wanted:
        return None

    candidates = [
        printer
        for printer in printers
        if str(printer.get("model") or "").strip().casefold() in wanted
        and str(printer.get("id") or "").lstrip("-").isdigit()
    ]
    if not candidates:
        return None

    def rank(printer: dict) -> tuple[int, int, int]:
        online = bool(printer.get("online"))
        busy = str(printer.get("status") or "").strip().casefold() in BUSY_STATUSES
        printer_id = int(printer["id"])
        return (
            0 if online and not busy else 1 if online else 2,
            (placed or {}).get(printer_id, 0),
            printer_id,
        )

    return int(sorted(candidates, key=rank)[0]["id"])


# Printer states that mean "this one is mid-job". Anything else — idle, ready,
# unknown — counts as free, because refusing to place work on a machine whose
# status we cannot read would stall the queue on a vocabulary mismatch.
BUSY_STATUSES = {"printing", "running", "busy", "working", "paused"}


def choose_candidate(
    candidates: list[dict],
    printers: list[dict],
    placed: dict[int, int] | None = None,
) -> tuple[dict, int | None] | None:
    """Which of a plate's files to print, and where — or None if nowhere.

    A product sliced for four machines has four files, and they are alternatives
    rather than a sequence: the right one is whichever names a printer that is
    free right now. So every candidate is costed and the cheapest wins, where
    cheap means "on a machine this pass has not already loaded up".

    A file that names its machines is preferred over one that says "anything",
    because naming them is somebody's decision and "anything" is the absence of
    one. Nothing available anywhere is None, and the plate waits.
    """
    best: tuple[tuple[int, int, int], dict, int | None] | None = None
    for index, candidate in enumerate(candidates):
        models = [str(m) for m in (candidate.get("printer_models") or [])]
        pinned = candidate.get("printer_id")
        if pinned is not None:
            # The file lives on that machine; there is nothing to choose.
            printer_id: int | None = int(pinned)
        elif models:
            printer_id = choose_printer(models, printers, placed)
            if printer_id is None:
                continue
        else:
            # No machine named: Bambuddy places it.
            printer_id = None
        targeted = 0 if (models or pinned is not None) else 1
        load = (placed or {}).get(printer_id, 0) if printer_id is not None else 0
        rank = (targeted, load, index)
        if best is None or rank < best[0]:
            best = (rank, candidate, printer_id)
    return None if best is None else (best[1], best[2])


def wanted_printers(candidates: list[dict]) -> str:
    """What a plate was looking for, for an error somebody has to read."""
    models: list[str] = []
    for candidate in candidates:
        for model in candidate.get("printer_models") or []:
            if str(model) not in models:
                models.append(str(model))
    return ", ".join(models) or "any printer"


async def dispatch_pending(session: AsyncSession, *, limit: int = 100) -> dict[str, int]:
    """Push pending jobs onto the Bambuddy queue."""
    # Never print for a cancelled line. Cancelling did not delete the plates it
    # had already planned, so without this an order cancelled minutes after it
    # arrived still goes on a printer — filament and a machine slot spent on
    # something nobody is going to send. Covers a line cancelled on its own and
    # a whole order cancelled by hand, because both land in the same state.
    pending = (
        (
            await session.execute(
                select(PrintJob)
                .join(OrderLine, OrderLine.id == PrintJob.order_line_id)
                .where(
                    PrintJob.status == JOB_PENDING,
                    OrderLine.state != LINE_CANCELLED,
                )
                # Oldest first. Unordered, the queue went out in whatever order
                # the database felt like, which is neither what an operator
                # expects nor stable enough to reason about.
                .order_by(PrintJob.created_at, PrintJob.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    stats = {"dispatched": 0, "failed": 0}
    if not pending:
        return stats

    try:
        client = await bambuddy_api.client_for(session)
    except IntegrationNotConfigured:
        return stats

    touched_orders: set = set()

    # Only read the farm if some job actually asks for a model. A shop that says
    # "any printer" everywhere should not pay for a printer listing on every
    # dispatch.
    printers: list[dict] | None = None
    placed: dict[int, int] = {}
    if any(
        any(c.get("printer_models") and c.get("printer_id") is None for c in job.candidates or [])
        for job in pending
    ):
        try:
            printers = await bambuddy_api.with_healing(
                session, client, ("printers",), client.list_printers
            )
        except IntegrationError as exc:
            log.warning("Could not read printers to place jobs by model: %s", exc)
            printers = []

    for job in pending:
        chosen = choose_candidate(job.candidates or [], printers or [], placed)
        if chosen is None:
            # Sending it anyway would put the plate on a machine that cannot
            # make it. Leave it pending and say what it was looking for; the
            # next poll tries again, and the operator can see why.
            job.error = (
                "No printer of "
                + wanted_printers(job.candidates or [])
                + " is available — leaving this plate queued here."
            )
            stats["failed"] += 1
            continue
        candidate, printer_id = chosen

        # What was chosen is recorded on the job, so the queue screen shows the
        # file that is actually printing rather than the list it came from.
        job.bambuddy_archive_id = candidate.get("archive_id")
        job.bambuddy_file_path = candidate.get("file_path")
        job.plate_number = candidate.get("plate_number") or 1
        job.printer_models = [str(m) for m in (candidate.get("printer_models") or [])]
        job.printer_id = printer_id
        if printer_id is not None:
            placed[printer_id] = placed.get(printer_id, 0) + 1

        try:
            # A 404 here is the same fault the file picker already heals: a
            # queue path nobody chose, because the instance keeps it somewhere
            # this connection never discovered. Nothing was created by a 404, so
            # re-reading the document and sending once more cannot double-queue.
            item = await bambuddy_api.with_healing(
                session,
                client,
                ("queue",),
                lambda: client.enqueue(
                    archive_id=job.bambuddy_archive_id,
                    file_path=job.bambuddy_file_path,
                    plate_number=job.plate_number,
                    printer_id=printer_id,
                    print_options=candidate.get("print_options") or {},
                ),
            )
        except IntegrationError as exc:
            job.error = str(exc)[:2000]
            stats["failed"] += 1
            await credentials.mark_error(session, PROVIDER_BAMBUDDY, str(exc))
            continue
        queue_id = item.get("id")
        job.bambuddy_queue_id = int(queue_id) if str(queue_id or "").isdigit() else None
        job.status = JOB_QUEUED
        job.queued_at = datetime.now(timezone.utc)
        job.error = None
        stats["dispatched"] += 1
        touched_orders.add(await _order_id_for_job(session, job))

    await session.flush()
    if stats["dispatched"]:
        await credentials.mark_ok(session, PROVIDER_BAMBUDDY)
    for order_id in touched_orders:
        if order_id is not None:
            await recompute_order_by_id(session, order_id)
    return stats


async def reconcile(session: AsyncSession) -> dict[str, int]:
    """Poll Bambuddy and advance every open job to its current status."""
    open_jobs = (
        (
            await session.execute(
                select(PrintJob).where(PrintJob.status.in_((JOB_QUEUED, JOB_PRINTING)))
            )
        )
        .scalars()
        .all()
    )
    stats = {"checked": len(open_jobs), "advanced": 0, "failed": 0, "done": 0}
    if not open_jobs:
        return stats

    client = await bambuddy_api.client_for(session)
    queue = await bambuddy_api.with_healing(
        session, client, ("queue",), client.list_queue
    )
    by_id = {
        int(item["id"]): item
        for item in queue
        if str(item.get("id") or "").lstrip("-").isdigit()
    }

    touched_orders: set = set()
    now = datetime.now(timezone.utc)

    for job in open_jobs:
        if job.bambuddy_queue_id is None:
            continue
        item = by_id.get(job.bambuddy_queue_id)
        if item is None:
            # Bambuddy drops finished items off the queue; treat a vanished job
            # that was already printing as complete, and leave a queued one alone
            # so a slow queue read never invents a false success.
            if job.status == JOB_PRINTING:
                job.status = JOB_DONE
                job.completed_at = now
                stats["done"] += 1
                stats["advanced"] += 1
                touched_orders.add(await _order_id_for_job(session, job))
            continue

        new_status = bambuddy_api.normalize_status(item.get("status"))
        if new_status is None or new_status == job.status:
            continue
        job.status = new_status
        if new_status == JOB_DONE:
            job.completed_at = now
            job.error = None
            stats["done"] += 1
        elif new_status in (JOB_FAILED, JOB_CANCELLED):
            job.completed_at = now
            job.error = str(item.get("error") or f"Bambuddy reported {item.get('status')}")[
                :2000
            ]
            stats["failed"] += 1
        stats["advanced"] += 1
        touched_orders.add(await _order_id_for_job(session, job))

    await session.flush()
    for order_id in touched_orders:
        if order_id is not None:
            await recompute_order_by_id(session, order_id)
    return stats


async def requeue_job(session: AsyncSession, job: PrintJob) -> PrintJob:
    """One-click re-queue for a failed or cancelled plate."""
    job.status = JOB_PENDING
    job.bambuddy_queue_id = None
    job.queued_at = None
    job.completed_at = None
    job.error = None
    await session.flush()
    await dispatch_pending(session)
    await session.refresh(job)
    return job


async def cancel_job(session: AsyncSession, job: PrintJob) -> PrintJob:
    if job.bambuddy_queue_id is not None:
        try:
            client = await bambuddy_api.client_for(session)
            await client.cancel(job.bambuddy_queue_id)
        except (IntegrationError, IntegrationNotConfigured) as exc:
            # Local state still moves; the queue item may already be gone.
            log.warning("Bambuddy cancel failed for queue %s: %s", job.bambuddy_queue_id, exc)
    job.status = JOB_CANCELLED
    job.completed_at = datetime.now(timezone.utc)
    await session.flush()
    order_id = await _order_id_for_job(session, job)
    if order_id is not None:
        await recompute_order_by_id(session, order_id)
    return job


async def _order_id_for_job(session: AsyncSession, job: PrintJob):
    line = await session.get(OrderLine, job.order_line_id)
    return line.order_id if line else None


async def open_jobs_overview(session: AsyncSession) -> list[dict]:
    """Every plate across all orders, newest first — what the farm screen groups."""
    rows = (
        (
            await session.execute(
                select(PrintJob)
                .options(
                    selectinload(PrintJob.order_line)
                    .selectinload(OrderLine.order),
                    selectinload(PrintJob.order_line).selectinload(OrderLine.product),
                )
                .order_by(PrintJob.created_at.desc())
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for job in rows:
        line = job.order_line
        order: Order | None = line.order if line else None
        out.append(
            {
                "id": job.id,
                "status": job.status,
                "bambuddy_queue_id": job.bambuddy_queue_id,
                "bambuddy_archive_id": job.bambuddy_archive_id,
                "bambuddy_file_path": job.bambuddy_file_path,
                "plate_number": job.plate_number,
                "printer_id": job.printer_id,
                "file_label": job.file_label,
                "printer_models": list(job.printer_models or []),
                "units_expected": job.units_expected,
                "queued_at": job.queued_at,
                "completed_at": job.completed_at,
                "error": job.error,
                "created_at": job.created_at,
                "order_id": order.id if order else None,
                "order_number": order.order_number if order else None,
                "buyer_name": order.buyer_name if order else None,
                "sku": (line.product.sku if line and line.product else (line.sku_raw if line else None)),
                "product_name": line.product.name if line and line.product else None,
            }
        )
    return out
