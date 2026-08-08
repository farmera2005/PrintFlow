"""Print-or-pull decisioning against QuickBooks Online quantity on hand (§4.2)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import qbo as qbo_api
from ..integrations.base import IntegrationError
from ..models import (
    RESERVING_LINE_STATES,
    OrderLine,
    Product,
)
from ..services import credentials
from ..services.credentials import IntegrationNotConfigured

log = logging.getLogger("printflow.allocation")


def split_quantity(
    quantity: int, qty_on_hand: float | None, reserved_elsewhere: int
) -> tuple[int, int]:
    """Return (qty_from_stock, qty_to_print) for one component line.

    The soft-reservation subtraction is what stops two simultaneous orders from
    both claiming the same last unit.
    """
    if qty_on_hand is None:
        return 0, quantity
    available = int(qty_on_hand) - reserved_elsewhere
    from_stock = min(quantity, max(available, 0))
    return from_stock, quantity - from_stock


async def reserved_quantities(
    session: AsyncSession, product_ids: list, exclude_line_ids: list | None = None
) -> dict:
    """Sum of stock already claimed by other open lines, per product."""
    if not product_ids:
        return {}
    stmt = (
        select(OrderLine.product_id, func.coalesce(func.sum(OrderLine.qty_from_stock), 0))
        .where(
            OrderLine.product_id.in_(product_ids),
            OrderLine.state.in_(RESERVING_LINE_STATES),
        )
        .group_by(OrderLine.product_id)
    )
    if exclude_line_ids:
        stmt = stmt.where(OrderLine.id.notin_(exclude_line_ids))
    rows = (await session.execute(stmt)).all()
    return {product_id: int(total or 0) for product_id, total in rows}


async def decide_lines(session: AsyncSession, lines: list[OrderLine]) -> None:
    """Set qty_from_stock / qty_to_print on each produced (non-bundle) line.

    Idempotent: re-running on already-decided lines recomputes the same split,
    because a line's own reservation is excluded from the availability maths.
    """
    targets = [line for line in lines if line.product_id is not None]
    if not targets:
        return

    products = {
        product.id: product
        for product in (
            await session.execute(
                select(Product).where(Product.id.in_([line.product_id for line in targets]))
            )
        )
        .scalars()
        .all()
    }
    # Bundle containers are never allocated or printed directly.
    targets = [
        line
        for line in targets
        if products.get(line.product_id) is not None
        and products[line.product_id].fulfillment != "bundle"
    ]
    if not targets:
        return

    needs_qbo = [
        line
        for line in targets
        if not line.force_print and products[line.product_id].qbo_item_id
    ]
    item_ids = sorted({products[line.product_id].qbo_item_id for line in needs_qbo})

    items: dict[str, dict] = {}
    qbo_error: str | None = None
    if item_ids:
        try:
            client = await qbo_api.client_for(session)
            items = await client.get_items(list(item_ids))
            await credentials.mark_ok(session, "qbo")
        except IntegrationNotConfigured:
            qbo_error = "QuickBooks is not connected"
        except IntegrationError as exc:
            qbo_error = str(exc)
            await credentials.mark_error(session, "qbo", str(exc))
        if qbo_error:
            log.warning("QBO lookup failed during decisioning: %s", qbo_error)

    reserved = await reserved_quantities(
        session,
        [line.product_id for line in targets],
        exclude_line_ids=[line.id for line in targets],
    )
    now = datetime.now(timezone.utc)

    for line in targets:
        product = products[line.product_id]
        note: str | None = None

        if line.force_print:
            # Operator override: "skip stock, print anyway".
            line.qty_from_stock = 0
            line.qty_to_print = line.quantity
            note = "Stock check skipped by manual override — printing full quantity."
        elif product.fulfillment == "stocked":
            # A stocked SKU has no print path; we always commit the full quantity
            # and warn if QuickBooks says there isn't enough.
            line.qty_from_stock = line.quantity
            line.qty_to_print = 0
            qty = qbo_api.item_qty_on_hand(items.get(str(product.qbo_item_id), {}))
            if qbo_error:
                note = f"Stock not verified: {qbo_error}."
            elif product.qbo_item_id is None:
                note = "No QuickBooks item linked — stock not verified."
            elif qty is None:
                note = "QuickBooks item is not inventory-tracked — stock not verified."
            else:
                available = int(qty) - reserved.get(line.product_id, 0)
                if available < line.quantity:
                    note = (
                        f"Short on stock: {max(available, 0)} available in QuickBooks, "
                        f"{line.quantity} needed."
                    )
        elif not product.qbo_item_id:
            # §4.2: a printed product with no QBO item skips the stock check.
            line.qty_from_stock = 0
            line.qty_to_print = line.quantity
        elif qbo_error:
            line.qty_from_stock = 0
            line.qty_to_print = line.quantity
            note = f"{qbo_error} — printing full quantity."
        else:
            item = items.get(str(product.qbo_item_id))
            if item is None:
                line.qty_from_stock = 0
                line.qty_to_print = line.quantity
                note = (
                    f"QuickBooks item {product.qbo_item_id} not found — printing full quantity."
                )
            else:
                qty = qbo_api.item_qty_on_hand(item)
                from_stock, to_print = split_quantity(
                    line.quantity, qty, reserved.get(line.product_id, 0)
                )
                line.qty_from_stock = from_stock
                line.qty_to_print = to_print
                if qty is None:
                    note = "QuickBooks item is not inventory-tracked — printing full quantity."

        # Claim what this line just took so the next line in the same batch sees it.
        reserved[line.product_id] = reserved.get(line.product_id, 0) + line.qty_from_stock
        line.stock_note = note
        line.decided_at = now

    await session.flush()
