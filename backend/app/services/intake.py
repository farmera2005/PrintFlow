"""Etsy receipt intake: ingest → SKU match → bundle explosion → decisioning (§4.1)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    LINE_EXPLODED,
    LINE_NEW,
    LINE_UNMATCHED,
    BomLine,
    Order,
    OrderLine,
    Product,
)
from ..services import allocation, printing
from ..services.state import recompute_order

log = logging.getLogger("printflow.intake")


def normalize_sku(raw: str | None) -> str:
    return (raw or "").strip()


def extract_sku(transaction: dict[str, Any]) -> str | None:
    """Etsy puts the SKU on the transaction, or inside product_data variations."""
    sku = normalize_sku(transaction.get("sku"))
    if sku:
        return sku
    product_data = transaction.get("product_data") or {}
    if isinstance(product_data, dict):
        sku = normalize_sku(product_data.get("sku"))
        if sku:
            return sku
        for offering in product_data.get("offerings") or []:
            if isinstance(offering, dict):
                sku = normalize_sku(offering.get("sku"))
                if sku:
                    return sku
    return None


def extract_buyer_name(receipt: dict[str, Any]) -> str | None:
    for key in ("name", "buyer_email", "first_line"):
        value = receipt.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _placed_at(receipt: dict[str, Any]) -> datetime | None:
    raw = receipt.get("created_timestamp") or receipt.get("create_timestamp")
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


async def match_product(session: AsyncSession, sku: str | None) -> Product | None:
    """Case-insensitive, whitespace-trimmed SKU match (§4.1)."""
    cleaned = normalize_sku(sku)
    if not cleaned:
        return None
    return (
        await session.execute(
            select(Product).where(func.lower(Product.sku) == cleaned.lower())
        )
    ).scalar_one_or_none()


async def ingest_receipt(session: AsyncSession, receipt: dict[str, Any]) -> tuple[Order, bool]:
    """Upsert an Etsy receipt. Returns (order, created). Idempotent (§6)."""
    receipt_id = receipt.get("receipt_id")
    if receipt_id is None:
        raise ValueError("Etsy receipt has no receipt_id")
    receipt_id = int(receipt_id)

    order = (
        await session.execute(select(Order).where(Order.etsy_receipt_id == receipt_id))
    ).scalar_one_or_none()

    if order is not None:
        # Already ingested — refresh the stored payload and stop. Re-running
        # intake on a processed receipt must not duplicate lines.
        order.raw = receipt
        order.buyer_name = extract_buyer_name(receipt) or order.buyer_name
        await session.flush()
        return order, False

    order = Order(
        etsy_receipt_id=receipt_id,
        order_number=str(receipt_id),
        buyer_name=extract_buyer_name(receipt),
        placed_at=_placed_at(receipt),
        raw=receipt,
    )
    session.add(order)
    await session.flush()

    for transaction in receipt.get("transactions") or []:
        if not isinstance(transaction, dict):
            continue
        sku = extract_sku(transaction)
        quantity = int(transaction.get("quantity") or 1)
        line = OrderLine(
            order_id=order.id,
            sku_raw=sku,
            title=transaction.get("title"),
            quantity=max(1, quantity),
            etsy_listing_id=_as_int(transaction.get("listing_id")),
            etsy_transaction_id=_as_int(transaction.get("transaction_id")),
            state=LINE_NEW,
        )
        session.add(line)

    await session.flush()
    await process_order(session, order)
    return order, True


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def process_order(session: AsyncSession, order: Order) -> Order:
    """Run the full intake pipeline for one order. Safe to re-run."""
    top_lines = (
        (
            await session.execute(
                select(OrderLine).where(
                    OrderLine.order_id == order.id, OrderLine.parent_line_id.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )

    produced: list[OrderLine] = []
    for line in top_lines:
        produced.extend(await _resolve_line(session, line))

    await allocation.decide_lines(session, produced)
    await printing.plan_jobs(session, produced)
    await recompute_order(session, order)
    return order


async def _resolve_line(session: AsyncSession, line: OrderLine) -> list[OrderLine]:
    """Match a top-level line to a product and explode it if it is a bundle.

    Returns the lines that actually get produced (components for a bundle, the
    line itself otherwise).
    """
    if line.product_id is None:
        product = await match_product(session, line.sku_raw)
        if product is None:
            line.state = LINE_UNMATCHED
            await session.flush()
            return []
        line.product_id = product.id
    else:
        product = await session.get(Product, line.product_id)
        if product is None:
            line.state = LINE_UNMATCHED
            await session.flush()
            return []

    if product.fulfillment != "bundle":
        if line.state == LINE_UNMATCHED:
            line.state = LINE_NEW
        await session.flush()
        return [line]

    # Bundle: the parent becomes a roll-up container and is never produced.
    line.state = LINE_EXPLODED
    existing = (
        (
            await session.execute(
                select(OrderLine)
                .where(OrderLine.parent_line_id == line.id)
                .options(selectinload(OrderLine.print_jobs))
            )
        )
        .scalars()
        .all()
    )
    by_product = {child.product_id: child for child in existing}

    bom = (
        (
            await session.execute(
                select(BomLine)
                .where(BomLine.bundle_id == product.id)
                .options(selectinload(BomLine.component))
            )
        )
        .scalars()
        .all()
    )
    if not bom:
        line.stock_note = "Bundle has no BOM components defined."
        await session.flush()
        return []

    children: list[OrderLine] = []
    for bom_line in bom:
        needed = line.quantity * bom_line.quantity
        child = by_product.get(bom_line.component_id)
        if child is None:
            child = OrderLine(
                order_id=line.order_id,
                parent_line_id=line.id,
                product_id=bom_line.component_id,
                sku_raw=bom_line.component.sku if bom_line.component else None,
                title=bom_line.component.name if bom_line.component else None,
                quantity=needed,
                state=LINE_NEW,
            )
            session.add(child)
        elif child.quantity != needed and not child.print_jobs:
            # BOM edited before anything was printed — resize the component line.
            child.quantity = needed
        children.append(child)

    await session.flush()
    return children


async def relink_line(
    session: AsyncSession, line: OrderLine, product: Product
) -> Order | None:
    """Fix an unmatched line by linking a product, then re-run intake for it."""
    line.product_id = product.id
    line.state = LINE_NEW
    await session.flush()
    order = await session.get(Order, line.order_id)
    if order is None:
        return None
    produced = await _resolve_line(session, line)
    await allocation.decide_lines(session, produced)
    await printing.plan_jobs(session, produced)
    await recompute_order(session, order)
    return order


async def redecide_line(session: AsyncSession, line: OrderLine) -> Order | None:
    """Re-run decisioning + plate planning for a single line (used by overrides)."""
    produced = [line]
    if line.product_id is not None:
        product = await session.get(Product, line.product_id)
        if product is not None and product.fulfillment == "bundle":
            produced = await _resolve_line(session, line)
    await allocation.decide_lines(session, produced)
    await printing.plan_jobs(session, produced)
    order = await session.get(Order, line.order_id)
    if order is not None:
        await recompute_order(session, order)
    return order


async def ingest_many(
    session: AsyncSession, receipts: list[dict[str, Any]]
) -> dict[str, int]:
    stats = {"seen": 0, "created": 0, "skipped": 0, "errors": 0}
    for receipt in receipts:
        stats["seen"] += 1
        try:
            _, created = await ingest_receipt(session, receipt)
        except Exception as exc:  # one bad receipt must not stop the poll
            stats["errors"] += 1
            log.exception("Failed to ingest Etsy receipt %s: %s", receipt.get("receipt_id"), exc)
            await session.rollback()
            continue
        stats["created" if created else "skipped"] += 1
        await session.commit()
    return stats
