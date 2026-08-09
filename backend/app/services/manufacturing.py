"""Made-items sheets: manufacturing into stock, posted to QuickBooks.

## What a posted sheet does to the books

One sheet becomes one QuickBooks Purchase (an Expense) carrying item lines:

* a **positive** line per product made — QuickBooks raises its quantity on hand
  and debits Inventory Asset;
* a **negative** line per component consumed, for products that have a BOM —
  quantity on hand falls and Inventory Asset is credited.

The two sides usually cancel: when a made item's cost is the roll-up of its
components, the Purchase totals zero and the sheet is a pure transfer of value
from components into finished goods. Where they do not cancel — someone typed a
higher cost to absorb labour or machine time — the difference lands in the
account chosen under Settings, which is what that setting is for.

## Why an Expense rather than an inventory adjustment

QuickBooks Online has no assembly build, and a journal entry cannot move an
inventory item's quantity. An item-based Purchase line is the documented way to
change quantity on hand, and it is the same mechanism QuickBooks uses when you
buy stock from a supplier.

## Rules that exist because this writes to real books

* Nothing posts without a person pressing Post. No scheduler touches this.
* A posted sheet is immutable. Corrections are a void plus a new sheet, so the
  ledger keeps both halves of the story.
* Posting carries a stable idempotency key, so a network timeout followed by a
  retry cannot produce two Purchases.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..integrations import qbo as qbo_api
from ..integrations.base import IntegrationError
from ..models import (
    BomLine,
    SHEET_DRAFT,
    SHEET_POSTED,
    SHEET_VOIDED,
    MadeSheet,
    MadeSheetLine,
    Product,
)
from . import audit
from .credentials import IntegrationNotConfigured
from .settings_store import get_manufacturing_settings

log = logging.getLogger("printflow.manufacturing")

CENTS = Decimal("0.01")


class SheetError(RuntimeError):
    """A sheet cannot be posted as it stands. The message is shown verbatim."""


def money(value: Decimal | float | int | str | None) -> Decimal:
    """Round to cents, half-up — the rounding people expect on an invoice."""
    if value is None:
        return Decimal("0.00")
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------
# Costing
# --------------------------------------------------------------------------


async def bom_unit_cost(
    session: AsyncSession, product: Product, costs: dict[str, Decimal]
) -> Decimal | None:
    """Roll a bundle's component costs up into one unit cost.

    `costs` maps a QuickBooks item id to its purchase cost. Returns None when
    the product has no BOM, or when a component's cost is unknown — half a
    roll-up is worse than none, because it looks like a real figure.
    """
    if not product.bom_lines:
        return None
    total = Decimal("0")
    for line in product.bom_lines:
        item_id = line.component.qbo_item_id
        if not item_id or item_id not in costs:
            return None
        total += costs[item_id] * line.quantity
    return money(total)


async def component_costs(
    session: AsyncSession, products: list[Product]
) -> dict[str, Decimal]:
    """Fetch the QuickBooks purchase cost of every component of every product."""
    item_ids = [
        line.component.qbo_item_id
        for product in products
        for line in product.bom_lines
        if line.component.qbo_item_id
    ]
    if not item_ids:
        return {}
    client = await qbo_api.client_for(session)
    items = await client.get_items(item_ids)
    out: dict[str, Decimal] = {}
    for item_id, item in items.items():
        cost = qbo_api.item_purchase_cost(item)
        if cost is not None:
            out[item_id] = cost
    return out


async def suggest_unit_cost(session: AsyncSession, product: Product) -> Decimal | None:
    """The BOM roll-up used to prefill a new line, or None if it cannot be had."""
    product = await loaded_product(session, product.id) or product
    try:
        costs = await component_costs(session, [product])
    except (IntegrationError, IntegrationNotConfigured) as exc:
        # QuickBooks being unreachable costs the operator a prefilled figure,
        # not the ability to build the sheet. Anything *else* thrown here is a
        # bug, and swallowing it would turn it into a silent zero on a line that
        # ends up in someone's accounts — so only these two are caught.
        log.info("Could not price %s from its BOM: %s", product.sku, exc)
        return None
    return await bom_unit_cost(session, product, costs)


# --------------------------------------------------------------------------
# What a sheet would do
# --------------------------------------------------------------------------


def explode_components(lines: list[MadeSheetLine]) -> dict[uuid.UUID, tuple[Product, int]]:
    """Total components consumed, keyed by product id.

    Single-level, matching the BOM rule everywhere else in the app. A product
    with no BOM consumes nothing — it is treated as made from materials that are
    expensed on purchase rather than tracked as stock.
    """
    consumed: dict[uuid.UUID, tuple[Product, int]] = {}
    for line in lines:
        for bom in line.product.bom_lines:
            product = bom.component
            _, running = consumed.get(product.id, (product, 0))
            consumed[product.id] = (product, running + bom.quantity * line.quantity)
    return consumed


async def preview(session: AsyncSession, sheet: MadeSheet) -> dict[str, Any]:
    """Everything the sheet would do, without doing any of it.

    Shown before posting because the operator is the only one who can tell a
    correct posting from a plausible one, and they cannot do that from a button
    labelled Post.
    """
    sheet = await loaded(session, sheet)
    settings = await get_manufacturing_settings(session)
    made: list[dict[str, Any]] = []
    made_total = Decimal("0")
    for line in sheet.lines:
        amount = money(Decimal(str(line.unit_cost)) * line.quantity)
        made_total += amount
        made.append(
            {
                "product_id": str(line.product_id),
                "sku": line.product.sku,
                "name": line.product.name,
                "quantity": line.quantity,
                "unit_cost": str(money(line.unit_cost)),
                "amount": str(amount),
                "qbo_item_id": line.product.qbo_item_id,
                "qbo_item_name": line.product.qbo_item_name,
            }
        )

    consumed: list[dict[str, Any]] = []
    consumed_total = Decimal("0")
    costs = await _safe_component_costs(session, [line.product for line in sheet.lines])
    for product, quantity in explode_components(sheet.lines).values():
        unit = costs.get(product.qbo_item_id or "")
        amount = money(unit * quantity) if unit is not None else None
        if amount is not None:
            consumed_total += amount
        consumed.append(
            {
                "product_id": str(product.id),
                "sku": product.sku,
                "name": product.name,
                "quantity": quantity,
                "unit_cost": str(money(unit)) if unit is not None else None,
                "amount": str(amount) if amount is not None else None,
                "qbo_item_id": product.qbo_item_id,
                "qbo_item_name": product.qbo_item_name,
            }
        )

    return {
        "made": made,
        "made_total": str(money(made_total)),
        "consumed": consumed,
        "consumed_total": str(money(consumed_total)),
        # What lands in the chosen account: added value (labour, overhead) when
        # positive, and a credit back when the made items were costed below
        # their components.
        "net_to_account": str(money(made_total - consumed_total)),
        "account_name": settings.get("account_name"),
        "problems": await problems(session, sheet, settings),
    }


async def _safe_component_costs(
    session: AsyncSession, products: list[Product]
) -> dict[str, Decimal]:
    try:
        return await component_costs(session, products)
    except (IntegrationError, IntegrationNotConfigured) as exc:
        # Same rule as suggest_unit_cost: an unreachable QuickBooks costs the
        # valuation, not the preview. Programming errors are left to surface.
        log.info("Could not price components: %s", exc)
        return {}


async def problems(
    session: AsyncSession, sheet: MadeSheet, settings: dict[str, Any] | None = None
) -> list[str]:
    """Everything that would make posting wrong, in the order worth fixing.

    Returned rather than raised so the UI can show them all at once instead of
    revealing them one failed post at a time.
    """
    sheet = await loaded(session, sheet)
    settings = settings if settings is not None else await get_manufacturing_settings(session)
    found: list[str] = []

    if sheet.status != SHEET_DRAFT:
        found.append(f"This sheet is already {sheet.status}.")
    if not sheet.lines:
        found.append("The sheet has no lines.")
    if not settings.get("account_id"):
        found.append(
            "No account is set for manufacturing cost. "
            "Choose one under Settings → QuickBooks."
        )

    for line in sheet.lines:
        if not line.product.qbo_item_id:
            found.append(
                f"{line.product.sku} is not linked to a QuickBooks item, so its "
                "quantity cannot be changed. Link it on the Products page."
            )
    for product, _ in explode_components(sheet.lines).values():
        if not product.qbo_item_id:
            found.append(
                f"{product.sku} is used as a component but is not linked to a "
                "QuickBooks item, so it cannot be consumed."
            )
    return found


# --------------------------------------------------------------------------
# Posting
# --------------------------------------------------------------------------


def build_purchase(
    sheet: MadeSheet,
    *,
    settings: dict[str, Any],
    component_unit_costs: dict[str, Decimal],
) -> dict[str, Any]:
    """The exact Purchase body sent to QuickBooks.

    Pure: no I/O, so the shape of what gets posted is testable without a
    QuickBooks company behind it.
    """
    lines: list[dict[str, Any]] = []

    for line in sheet.lines:
        unit = money(line.unit_cost)
        lines.append(
            _item_line(
                item_id=str(line.product.qbo_item_id),
                quantity=line.quantity,
                unit_cost=unit,
                amount=money(unit * line.quantity),
                description=f"Made {line.quantity} × {line.product.sku} — {line.product.name}",
                account_id=settings.get("account_id"),
            )
        )

    for product, quantity in explode_components(sheet.lines).values():
        unit = component_unit_costs.get(product.qbo_item_id or "")
        if unit is None:
            # Consuming at an unknown cost would post a zero-value credit and
            # quietly misstate inventory value. Skip the value, not the quantity:
            # QuickBooks still needs the negative quantity to lower stock.
            unit = Decimal("0")
        lines.append(
            _item_line(
                item_id=str(product.qbo_item_id),
                # Negative quantity is what lowers quantity on hand.
                quantity=-quantity,
                unit_cost=money(unit),
                amount=money(-(money(unit) * quantity)),
                description=f"Consumed {quantity} × {product.sku} — {product.name}",
                account_id=settings.get("account_id"),
            )
        )

    body: dict[str, Any] = {
        "AccountRef": {"value": str(settings["account_id"])},
        "PaymentType": str(settings.get("payment_type") or "Cash"),
        "TxnDate": sheet.made_on.date().isoformat(),
        "PrivateNote": _note(sheet),
        "Line": lines,
    }
    if settings.get("vendor_id"):
        body["EntityRef"] = {"value": str(settings["vendor_id"]), "type": "Vendor"}
    if settings.get("doc_number_prefix"):
        # QuickBooks caps DocNumber at 21 characters.
        body["DocNumber"] = f"{settings['doc_number_prefix']}{sheet.reference}"[:21]
    return body


def _item_line(
    *,
    item_id: str,
    quantity: int,
    unit_cost: Decimal,
    amount: Decimal,
    description: str,
    account_id: Any,
) -> dict[str, Any]:
    return {
        "DetailType": "ItemBasedExpenseLineDetail",
        "Amount": float(amount),
        "Description": description,
        "ItemBasedExpenseLineDetail": {
            "ItemRef": {"value": item_id},
            "Qty": quantity,
            "UnitPrice": float(unit_cost),
            "BillableStatus": "NotBillable",
        },
    }


def _note(sheet: MadeSheet) -> str:
    note = f"PrintFlow made-items sheet {sheet.reference}"
    if sheet.memo:
        note = f"{note} — {sheet.memo}"
    return note[:4000]


async def post(session: AsyncSession, sheet: MadeSheet, *, actor: str) -> MadeSheet:
    """Post the sheet to QuickBooks. Raises SheetError with a readable reason."""
    sheet = await loaded(session, sheet)
    settings = await get_manufacturing_settings(session)
    found = await problems(session, sheet, settings)
    if found:
        raise SheetError(" ".join(found))

    costs = await _safe_component_costs(session, [line.product for line in sheet.lines])
    body = build_purchase(sheet, settings=settings, component_unit_costs=costs)

    client = await qbo_api.client_for(session)
    try:
        created = await client.create_purchase(body, request_id=str(sheet.idempotency_key))
    except IntegrationError as exc:
        # Record the attempt: if this was a timeout the Purchase may exist, and
        # the payload plus the idempotency key are how that gets untangled.
        sheet.qbo_request = body
        await session.flush()
        raise SheetError(
            f"QuickBooks rejected the posting: {exc}. Nothing was saved as posted — "
            "check QuickBooks before trying again."
        ) from exc

    sheet.status = SHEET_POSTED
    sheet.qbo_purchase_id = str(created.get("Id") or "") or None
    sheet.qbo_doc_number = str(created.get("DocNumber") or "") or None
    sheet.qbo_sync_token = str(created.get("SyncToken") or "0")
    sheet.qbo_request = body
    sheet.qbo_response = created
    sheet.posted_at = datetime.now(timezone.utc)
    sheet.posted_by = actor
    await session.flush()

    await audit.record(
        session,
        entity_type="made_sheet",
        entity_id=sheet.id,
        action="posted_to_qbo",
        detail={
            "reference": sheet.reference,
            "qbo_purchase_id": sheet.qbo_purchase_id,
            "doc_number": sheet.qbo_doc_number,
            "lines": len(body["Line"]),
        },
        actor=actor,
    )
    return sheet


async def void(session: AsyncSession, sheet: MadeSheet, *, actor: str) -> MadeSheet:
    """Delete the Purchase in QuickBooks, reversing every quantity it moved."""
    if sheet.status != SHEET_POSTED:
        raise SheetError(f"Only a posted sheet can be voided; this one is {sheet.status}.")
    if not sheet.qbo_purchase_id:
        raise SheetError("This sheet has no QuickBooks transaction recorded against it.")

    client = await qbo_api.client_for(session)
    try:
        await client.void_purchase(sheet.qbo_purchase_id, sheet.qbo_sync_token or "0")
    except IntegrationError as exc:
        raise SheetError(f"QuickBooks would not delete the transaction: {exc}") from exc

    sheet.status = SHEET_VOIDED
    sheet.voided_at = datetime.now(timezone.utc)
    sheet.voided_by = actor
    await session.flush()

    await audit.record(
        session,
        entity_type="made_sheet",
        entity_id=sheet.id,
        action="voided_in_qbo",
        detail={"reference": sheet.reference, "qbo_purchase_id": sheet.qbo_purchase_id},
        actor=actor,
    )
    return sheet


# --------------------------------------------------------------------------
# Sheet housekeeping
# --------------------------------------------------------------------------


async def next_reference(session: AsyncSession) -> str:
    """Sequential, date-stamped, and readable in a QuickBooks register."""
    today = datetime.now(timezone.utc).date()
    prefix = f"MI-{today:%Y%m%d}"
    existing = (
        await session.execute(
            select(MadeSheet.reference).where(MadeSheet.reference.like(f"{prefix}-%"))
        )
    ).scalars().all()
    used = set()
    for reference in existing:
        tail = str(reference).rsplit("-", 1)[-1]
        if tail.isdigit():
            used.add(int(tail))
    return f"{prefix}-{(max(used) + 1) if used else 1:02d}"


# Every relationship the costing and the payload builder walk, loaded in one
# go. Spelled out rather than left to the mappers' defaults: an attribute that
# turns out not to be loaded raises MissingGreenlet the moment it is touched
# outside an await, and the place that would happen is halfway through building
# a transaction for someone's books.
_SHEET_GRAPH = (
    selectinload(MadeSheet.lines)
    .selectinload(MadeSheetLine.product)
    .selectinload(Product.bom_lines)
    .selectinload(BomLine.component)
)


async def get_sheet(session: AsyncSession, sheet_id: uuid.UUID) -> MadeSheet | None:
    """Re-read the sheet and everything hanging off it.

    populate_existing re-runs the loaders over an instance the identity map
    already holds. Without it a sheet fetched after a commit comes back expired,
    and the first attribute touched raises instead of quietly loading.
    """
    return (
        await session.execute(
            select(MadeSheet)
            .where(MadeSheet.id == sheet_id)
            .options(_SHEET_GRAPH)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def loaded(session: AsyncSession, sheet: MadeSheet) -> MadeSheet:
    """The same sheet, with its whole graph guaranteed present."""
    return await get_sheet(session, sheet.id) or sheet


async def loaded_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    """A product with its BOM and every component, ready to be priced."""
    return (
        await session.execute(
            select(Product)
            .where(Product.id == product_id)
            .options(selectinload(Product.bom_lines).selectinload(BomLine.component))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def assert_editable(sheet: MadeSheet) -> None:
    if sheet.status != SHEET_DRAFT:
        raise SheetError(
            f"This sheet is {sheet.status} and cannot be changed. "
            "Void it and make a new one to correct it."
        )
