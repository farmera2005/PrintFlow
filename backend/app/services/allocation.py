"""Print-or-pull decisioning against QuickBooks Online quantity on hand (§4.2)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import qbo as qbo_api
from ..integrations.base import IntegrationError
from ..models import (
    RESERVING_LINE_STATES,
    OrderLine,
    Product,
    ProductVariation,
)
from ..services import credentials, variations
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
    session: AsyncSession, exclude_line_ids: list | None = None
) -> dict[str, int]:
    """Stock already claimed by other open lines, keyed by QuickBooks item.

    Keyed by the item rather than the product because the two stopped being the
    same thing once a variation could name its own item: one colour of a
    printed part draws down its own stock, while two products pointed at one
    item are competing for the same units and have to see each other's claims.
    """
    item = func.coalesce(ProductVariation.qbo_item_id, Product.qbo_item_id)
    stmt = (
        select(item, func.coalesce(func.sum(OrderLine.qty_from_stock), 0))
        .select_from(OrderLine)
        .join(Product, Product.id == OrderLine.product_id)
        .outerjoin(ProductVariation, ProductVariation.id == OrderLine.variation_id)
        .where(OrderLine.state.in_(RESERVING_LINE_STATES), item.isnot(None))
        .group_by(item)
    )
    if exclude_line_ids:
        stmt = stmt.where(OrderLine.id.notin_(exclude_line_ids))
    rows = (await session.execute(stmt)).all()
    return {str(item_id): int(total or 0) for item_id, total in rows}


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

    # The variation, where there is one, decides which item this line draws on.
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
    item_of: dict[Any, str | None] = {}
    for line in targets:
        item_id, _name = variations.stock_item(
            products[line.product_id], variation_rows.get(line.variation_id)
        )
        item_of[line.id] = item_id

    needs_qbo = [
        line for line in targets if not line.force_print and item_of[line.id]
    ]
    item_ids = sorted({item_of[line.id] for line in needs_qbo})

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
        session, exclude_line_ids=[line.id for line in targets]
    )
    now = datetime.now(timezone.utc)

    for line in targets:
        product = products[line.product_id]
        item_id = item_of[line.id]
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
            qty = qbo_api.item_qty_on_hand(items.get(str(item_id), {}))
            if qbo_error:
                note = f"Stock not verified: {qbo_error}."
            elif item_id is None:
                note = "No QuickBooks item linked — stock not verified."
            elif qty is None:
                note = "QuickBooks item is not inventory-tracked — stock not verified."
            else:
                available = int(qty) - reserved.get(str(item_id), 0)
                if available < line.quantity:
                    note = (
                        f"Short on stock: {max(available, 0)} available in QuickBooks, "
                        f"{line.quantity} needed."
                    )
        elif not item_id:
            # §4.2: a printed product with no QBO item skips the stock check.
            line.qty_from_stock = 0
            line.qty_to_print = line.quantity
        elif qbo_error:
            line.qty_from_stock = 0
            line.qty_to_print = line.quantity
            note = f"{qbo_error} — printing full quantity."
        else:
            item = items.get(str(item_id))
            if item is None:
                line.qty_from_stock = 0
                line.qty_to_print = line.quantity
                note = (
                    f"QuickBooks item {item_id} not found — printing full quantity."
                )
            else:
                qty = qbo_api.item_qty_on_hand(item)
                from_stock, to_print = split_quantity(
                    line.quantity, qty, reserved.get(str(item_id), 0)
                )
                line.qty_from_stock = from_stock
                line.qty_to_print = to_print
                if qty is None:
                    note = "QuickBooks item is not inventory-tracked — printing full quantity."

        # Claim what this line just took so the next line in the same batch sees it.
        if item_id:
            reserved[str(item_id)] = reserved.get(str(item_id), 0) + line.qty_from_stock
        line.stock_note = note
        line.decided_at = now

    await session.flush()
