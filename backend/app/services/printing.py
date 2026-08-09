"""Print job planning, dispatch to Bambuddy, and status reconciliation (§4.3)."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

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
    PrintMapping,
    Product,
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

    mappings = {
        mapping.product_id: mapping
        for mapping in (
            await session.execute(
                select(PrintMapping).where(
                    PrintMapping.product_id.in_([line.product_id for line in targets])
                )
            )
        )
        .scalars()
        .all()
    }
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
        plan = variations.print_plan(mappings.get(line.product_id), variation)
        if plan is None:
            line.stock_note = (
                (line.stock_note + " ") if line.stock_note else ""
            ) + "No Bambuddy print mapping for this product — cannot queue prints."
            continue

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
        # printed. A job that has not reached Bambuddy is still only an
        # intention, so correct it; anything queued or printing is a fact on a
        # machine and hiding it would not unprint it.
        stale = [
            job
            for job in existing
            if job.status == JOB_PENDING
            and (
                job.bambuddy_archive_id != plan.bambuddy_archive_id
                or job.plate_number != plan.plate_number
            )
        ]
        for job in stale:
            await session.delete(job)
        if stale:
            existing = [job for job in existing if job not in stale]
            await session.flush()

        required = plates_needed(line.qty_to_print, plan.units_per_plate)
        for _ in range(max(0, required - len(existing))):
            job = PrintJob(
                order_line_id=line.id,
                bambuddy_archive_id=plan.bambuddy_archive_id,
                plate_number=plan.plate_number,
                printer_id=plan.preferred_printer_id,
                units_expected=plan.units_per_plate,
                status=JOB_PENDING,
            )
            session.add(job)
            created.append(job)

    await session.flush()
    return created


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

    mappings = await _mappings_for_jobs(session, pending)
    touched_orders: set = set()

    for job in pending:
        mapping = mappings.get(job.id)
        try:
            item = await client.enqueue(
                archive_id=job.bambuddy_archive_id,
                plate_number=job.plate_number,
                printer_id=job.printer_id,
                print_options=(mapping.print_options if mapping else None) or {},
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
    queue = await client.list_queue()
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


async def _mappings_for_jobs(
    session: AsyncSession, jobs: list[PrintJob]
) -> dict[object, PrintMapping]:
    """Map job.id → the PrintMapping that produced it (for print_options)."""
    line_ids = {job.order_line_id for job in jobs}
    if not line_ids:
        return {}
    rows = (
        await session.execute(
            select(OrderLine.id, PrintMapping)
            .join(Product, Product.id == OrderLine.product_id)
            .join(PrintMapping, PrintMapping.product_id == Product.id)
            .where(OrderLine.id.in_(line_ids))
        )
    ).all()
    by_line = {line_id: mapping for line_id, mapping in rows}
    return {job.id: by_line[job.order_line_id] for job in jobs if job.order_line_id in by_line}


async def _order_id_for_job(session: AsyncSession, job: PrintJob):
    line = await session.get(OrderLine, job.order_line_id)
    return line.order_id if line else None


async def open_jobs_overview(session: AsyncSession) -> list[dict]:
    """Flat Print Queue view across all orders."""
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
                "plate_number": job.plate_number,
                "printer_id": job.printer_id,
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
