"""Orders typed in by hand.

Not every order arrives through an API. A shop takes one over the phone, sells
from a stall, does a trade order, sends a replacement out free — and until now
none of those existed in PrintFlow, so none of them got a plate planned, a
label bought or an invoice raised. The work happened and the system did not
know about it.

**One pipeline, not two.** A manual order is an `Order` row like any other and
goes straight into `intake.process_order`, so its lines are matched to products,
its bundles explode, its stock is decided against QuickBooks and its plates are
planned by exactly the code a polled order runs. Everything downstream — the
board, the drawer, assembly, labels, invoicing, the books — needs to know
nothing about where it came from.

**What is different, and it is only this: nothing will ever update it.** A
polled order is re-read on every poll, which is how its address gets backfilled
and its fees arrive days later. There is no second look here, so what was typed
is the whole truth: the money is stored on the order rather than parsed out of a
payload, and each line carries its own price because there is no receipt to read
one back out of.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    LINE_NEW,
    ORDER_NEW,
    SOURCE_MANUAL,
    Order,
    OrderLine,
    Product,
)
from . import intake
from .manufacturing import money

log = logging.getLogger("printflow.manual")


class ManualOrderError(RuntimeError):
    """The order cannot be created as described. Shown verbatim."""


# What a generated reference looks like. A prefix, because an order number is
# read aloud and written on a box, and one that cannot be told apart from an
# Etsy receipt number is a number somebody will go looking for on Etsy.
REFERENCE_PREFIX = "M-"
FIRST_REFERENCE = 1001


async def next_reference(session: AsyncSession) -> str:
    """The next `M-` reference, counted from the highest already used.

    From the highest rather than from a count, so deleting nothing and
    cancelling something never hands out a reference twice. A race between two
    people pressing at once is caught by the unique index instead, which is the
    only thing that can actually be relied on.
    """
    used = (
        (
            await session.execute(
                select(Order.order_number).where(
                    Order.source == SOURCE_MANUAL,
                    Order.order_number.like(f"{REFERENCE_PREFIX}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    highest = FIRST_REFERENCE - 1
    for reference in used:
        tail = str(reference)[len(REFERENCE_PREFIX) :]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{REFERENCE_PREFIX}{highest + 1}"


def _amount(raw: Any, *, what: str) -> Decimal | None:
    """A typed figure, or a refusal naming the box it came from.

    Blank is None rather than zero: "no shipping was charged" and "shipping has
    not been filled in" are different, and only one of them should show as
    0.00 on an invoice.
    """
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise ManualOrderError(f"{what} is not a number: “{raw}”.") from exc
    if value < 0:
        raise ManualOrderError(f"{what} cannot be negative.")
    return money(value)


async def create_order(
    session: AsyncSession, payload: dict[str, Any]
) -> Order:
    """Create an order from typed-in detail and run it through intake.

    Raises ManualOrderError with a sentence worth showing. A person is filling
    in a form, so every refusal names the field and what is wrong with it.
    """
    lines = payload.get("lines") or []
    if not lines:
        raise ManualOrderError("An order needs at least one line.")

    reference = str(payload.get("order_number") or "").strip()
    if not reference:
        reference = await next_reference(session)

    buyer = str(payload.get("buyer_name") or "").strip() or None
    placed_at = payload.get("placed_at") or datetime.now(timezone.utc)
    if placed_at.tzinfo is None:
        placed_at = placed_at.replace(tzinfo=timezone.utc)

    # Read and checked before anything is added, so a bad figure on the third
    # line does not leave half an order behind.
    prepared: list[dict[str, Any]] = []
    for index, raw in enumerate(lines, start=1):
        product_id = raw.get("product_id")
        if not product_id:
            raise ManualOrderError(f"Line {index} has no product.")
        product = await session.get(Product, product_id)
        if product is None:
            raise ManualOrderError(f"Line {index} names a product that no longer exists.")
        try:
            quantity = int(raw.get("quantity") or 0)
        except (TypeError, ValueError) as exc:
            raise ManualOrderError(
                f"Line {index} has a quantity that is not a whole number."
            ) from exc
        if quantity < 1:
            raise ManualOrderError(f"Line {index} needs a quantity of at least one.")
        prepared.append(
            {
                "product": product,
                "quantity": quantity,
                "unit_price": _amount(
                    raw.get("unit_price"), what=f"The price on line {index}"
                ),
            }
        )

    shipping = _amount(payload.get("shipping_total"), what="Shipping")
    tax = _amount(payload.get("tax_total"), what="Tax")
    discount = _amount(payload.get("discount_total"), what="Discount")

    # What the buyer paid, worked out rather than typed again. Asking for a
    # total as well as the parts is asking for two numbers that disagree.
    items_total = sum(
        (row["unit_price"] * row["quantity"] for row in prepared if row["unit_price"]),
        Decimal(0),
    )
    revenue = items_total + (shipping or 0) + (tax or 0) - (discount or 0)

    order = Order(
        source=SOURCE_MANUAL,
        # The reference doubles as the channel id, so the partial unique index
        # on (source, external_id) refuses a second order with the same one.
        external_id=reference,
        order_number=reference,
        buyer_name=buyer,
        placed_at=placed_at,
        status=ORDER_NEW,
        ship_to=payload.get("ship_to") or None,
        currency=str(payload.get("currency") or "").strip() or None,
        items_total=money(items_total) if items_total else None,
        shipping_total=shipping,
        tax_total=tax,
        discount_total=discount,
        revenue=money(revenue) if revenue else None,
        # No payload. Nothing is going to re-read this order, and an empty
        # object here would make `finance.apply_stored` try — see its early
        # return, which is what keeps typed figures from being overwritten.
        raw=None,
    )
    session.add(order)
    await session.flush()

    for row in prepared:
        product = row["product"]
        session.add(
            OrderLine(
                order_id=order.id,
                product_id=product.id,
                # Matched already, by construction: the product was picked from
                # a list rather than guessed at from a SKU. `sku_raw` is still
                # filled in because the drawer and the audit trail read it.
                sku_raw=product.sku,
                title=product.name,
                quantity=row["quantity"],
                unit_price=row["unit_price"],
                state=LINE_NEW,
            )
        )
    await session.flush()

    # The same pipeline a polled order runs: bundles explode, stock is decided
    # against QuickBooks, plates are planned. A manual order is an order.
    await intake.process_order(session, order)
    return order
