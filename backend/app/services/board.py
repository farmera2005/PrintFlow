"""Read models for the flow board and the order detail drawer."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..integrations import bambuddy as bambuddy_api
from ..models import (
    BOARD_COLUMNS,
    COMPLETE_BOARD_HOURS,
    JOB_CANCELLED,
    JOB_FAILED,
    LINE_CANCELLED,
    LINE_UNMATCHED,
    ORDER_CANCELLED,
    Order,
    OrderLine,
    PrintJob,
)
from ..services import credentials, finance, tracking
from ..services.manufacturing import money
from ..services.state import suggested_status


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
            # Which variation of its product this is, so the operator can see
            # the automatch landed where they expect.
            "variation_label": line.variation.label if line.variation else None,
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
        "bambuddy_file_path": job.bambuddy_file_path,
        "plate_number": job.plate_number,
        "printer_id": job.printer_id,
        # Which of the product's files this plate is using, and what will take
        # it. With several files per product the product no longer says.
        "file_label": job.file_label,
        "printer_models": list(job.printer_models or []),
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


def _amount(value: Any) -> str | None:
    """Money to the screen: a decimal string, rounded to cents."""
    return None if value is None else str(money(value))


def order_card(order: Order) -> dict[str, Any]:
    return {
        "id": order.id,
        "order_number": order.order_number,
        "etsy_receipt_id": order.etsy_receipt_id,
        "buyer_name": order.buyer_name,
        "placed_at": order.placed_at,
        "status": order.status,
        "status_note": order.status_note,
        # Where the rules would put it. Nothing acts on this — the board shows
        # it only when it disagrees with where the card actually is.
        "suggested_status": suggested_status(order, list(order.lines)),
        "tracking_number": order.tracking_number,
        # A number nobody can click is a number somebody retypes into a
        # carrier's website. None where PrintFlow does not know that carrier's
        # page — a link to the wrong one looks like an answer.
        "tracking_url": tracking.tracking_url(
            order.carrier_code, order.tracking_number, order.tracking_url
        ),
        "tracking_status": order.tracking_status,
        "tracking_detail": order.tracking_detail,
        "tracking_checked_at": order.tracking_checked_at,
        "delivered_at": order.delivered_at,
        "completed_at": order.completed_at,
        "label_created_at": order.label_created_at,
        # A string, not a float: this is money on its way to a screen, and
        # JSON's only number is the one that cannot hold 7.41 exactly. Rounded
        # to cents on the way out like every other figure PrintFlow shows.
        "label_cost": str(money(order.label_cost)) if order.label_cost is not None else None,
        "label_currency": order.label_currency,
        # What the order was worth and what selling it cost. All decimal
        # strings, all in `currency`; see the finance service for why the fees
        # arrive later than everything else.
        "currency": order.currency,
        **{
            field: _amount(getattr(order, field))
            for field in (
                "revenue", "items_total", "shipping_total", "tax_total",
                "discount_total", "etsy_fees", "marketing_fees", "processing_fees",
            )
        },
        "net": _amount(finance.net_of(order)),
        "finance_synced_at": order.finance_synced_at,
        "shipstation_order_id": order.shipstation_order_id,
        "summary": _summary(order),
        "lines": _line_tree(order),
    }


async def build_board(
    session: AsyncSession, *, limit_per_column: int = 100
) -> dict[str, Any]:
    orders = await _load_orders(session, BOARD_COLUMNS, limit_per_column * len(BOARD_COLUMNS))
    columns: dict[str, list[dict[str, Any]]] = {name: [] for name in BOARD_COLUMNS}
    retired = 0
    for order in orders:
        # A delivered card stops being drawn two days later. The order is not
        # touched — it is in the Orders tab with everything it ever had — but a
        # board that only grows is a board nobody scrolls to the bottom of.
        if not tracking.still_on_board(order):
            retired += 1
            continue
        bucket = columns.get(order.status)
        if bucket is not None and len(bucket) < limit_per_column:
            bucket.append(order_card(order))
    return {
        "columns": [
            {"key": name, "orders": columns[name], "count": len(columns[name])}
            for name in BOARD_COLUMNS
        ],
        # Said out loud, because a card that silently stopped being drawn is
        # indistinguishable from one that was deleted — and nothing here is
        # ever deleted.
        "retired_from_board": retired,
        "complete_board_hours": COMPLETE_BOARD_HOURS,
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
    # The address and the fee lines are drawer-sized, not card-sized: one is
    # read when packing and the other when somebody queries a number.
    payload["ship_to"] = order.ship_to
    payload["fee_lines"] = order.fee_lines or []
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
