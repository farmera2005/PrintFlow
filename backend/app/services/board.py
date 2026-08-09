"""Read models for the flow board and the order detail drawer."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..integrations import bambuddy as bambuddy_api
from ..models import (
    BOARD_COLUMNS,
    JOB_CANCELLED,
    JOB_FAILED,
    LINE_CANCELLED,
    LINE_UNMATCHED,
    ORDER_CANCELLED,
    Order,
    OrderLine,
    PrintJob,
)
from ..services import credentials


async def _load_orders(session: AsyncSession, statuses: tuple[str, ...], limit: int):
    return (
        (
            await session.execute(
                select(Order)
                .where(Order.status.in_(statuses))
                .options(
                    selectinload(Order.lines).selectinload(OrderLine.print_jobs),
                    selectinload(Order.lines).selectinload(OrderLine.product),
                )
                .order_by(Order.placed_at.desc().nullslast(), Order.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


def _line_tree(order: Order) -> list[dict[str, Any]]:
    children: dict[Any, list[OrderLine]] = {}
    roots: list[OrderLine] = []
    for line in order.lines:
        if line.parent_line_id is None:
            roots.append(line)
        else:
            children.setdefault(line.parent_line_id, []).append(line)

    def serialize(line: OrderLine) -> dict[str, Any]:
        kids = children.get(line.id, [])
        return {
            "id": line.id,
            "parent_line_id": line.parent_line_id,
            "product_id": line.product_id,
            "sku": (line.product.sku if line.product else line.sku_raw),
            "sku_raw": line.sku_raw,
            "name": (line.product.name if line.product else line.title),
            "fulfillment": line.product.fulfillment if line.product else None,
            "quantity": line.quantity,
            "qty_from_stock": line.qty_from_stock,
            "qty_to_print": line.qty_to_print,
            "state": line.state,
            "override_state": line.override_state,
            "force_print": line.force_print,
            "stock_note": line.stock_note,
            # What the buyer picked, and what those picks did to the BOM. Both
            # belong on the card: the operator making the thing needs to see
            # "Color: Red" without opening Etsy.
            "variations": line.variations or [],
            "option_effects": line.option_effects or [],
            # The listing this came from, so an unmatched line can offer to
            # remember the link — a listing with no SKU has nothing else.
            "etsy_listing_id": line.etsy_listing_id,
            "etsy_product_id": line.etsy_product_id,
            "assembled_at": line.assembled_at,
            "is_bundle": bool(kids),
            "print_jobs": [_job(job) for job in sorted(line.print_jobs, key=lambda j: j.created_at)],
            "children": [serialize(kid) for kid in sorted(kids, key=lambda c: c.created_at)],
        }

    return [serialize(line) for line in sorted(roots, key=lambda line: line.created_at)]


def _job(job: PrintJob) -> dict[str, Any]:
    return {
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
    }


def _summary(order: Order) -> dict[str, Any]:
    active = [line for line in order.lines if line.state != LINE_CANCELLED]
    unmatched = sum(1 for line in active if line.state == LINE_UNMATCHED)
    failed_jobs = sum(
        1
        for line in active
        for job in line.print_jobs
        if job.status in (JOB_FAILED, JOB_CANCELLED)
    )
    child_ids = {line.parent_line_id for line in order.lines if line.parent_line_id}
    return {
        "line_count": len([line for line in active if line.id not in child_ids]),
        "unmatched_count": unmatched,
        "failed_job_count": failed_jobs,
        "needs_attention": bool(unmatched or failed_jobs),
        "units_from_stock": sum(line.qty_from_stock for line in active),
        "units_to_print": sum(line.qty_to_print for line in active),
        "pending_assembly": [
            {"line_id": line.id, "sku": line.product.sku if line.product else line.sku_raw}
            for line in order.lines
            if line.id in child_ids and line.assembled_at is None
        ],
    }


def order_card(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "etsy_receipt_id": order.etsy_receipt_id,
        "buyer_name": order.buyer_name,
        "placed_at": order.placed_at,
        "status": order.status,
        "tracking_number": order.tracking_number,
        "label_created_at": order.label_created_at,
        "shipstation_order_id": order.shipstation_order_id,
        "summary": _summary(order),
        "lines": _line_tree(order),
    }


async def build_board(
    session: AsyncSession, *, limit_per_column: int = 100
) -> dict[str, Any]:
    orders = await _load_orders(session, BOARD_COLUMNS, limit_per_column * len(BOARD_COLUMNS))
    columns: dict[str, list[dict[str, Any]]] = {name: [] for name in BOARD_COLUMNS}
    for order in orders:
        bucket = columns.get(order.status)
        if bucket is not None and len(bucket) < limit_per_column:
            bucket.append(order_card(order))
    return {
        "columns": [
            {"key": name, "orders": columns[name], "count": len(columns[name])}
            for name in BOARD_COLUMNS
        ],
        "integrations": await credentials.status_summary(session),
    }


async def load_order_detail(session: AsyncSession, order_id) -> dict[str, Any] | None:
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id)
            .options(
                selectinload(Order.lines).selectinload(OrderLine.print_jobs),
                selectinload(Order.lines).selectinload(OrderLine.product),
            )
        )
    ).scalar_one_or_none()
    if order is None:
        return None
    payload = order_card(order)
    payload["raw_available"] = order.raw is not None
    payload["cancelled"] = order.status == ORDER_CANCELLED
    payload["carrier_code"] = order.carrier_code
    payload["service_code"] = order.service_code
    payload["has_label_pdf"] = order.label_pdf is not None
    bambuddy = await credentials.load(session, "bambuddy")
    base_url = (bambuddy.get("base_url") or "").rstrip("/") if bambuddy else ""
    payload["bambuddy_base_url"] = base_url or None
    if base_url:
        client = bambuddy_api.BambuddyClient(bambuddy)
        _attach_job_links(payload["lines"], client)
    return payload


def _attach_job_links(lines: list[dict[str, Any]], client: bambuddy_api.BambuddyClient) -> None:
    for line in lines:
        for job in line["print_jobs"]:
            job["bambuddy_url"] = client.queue_item_url(job.get("bambuddy_queue_id"))
        _attach_job_links(line["children"], client)
