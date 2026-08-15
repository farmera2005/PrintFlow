"""What the order pipeline writes into QuickBooks.

Until now nothing here wrote to the books at all: order handling read QtyOnHand
to decide print-or-pull (§4.2) and left it alone, and the only write in the
system was a manufacturing sheet raising stock. That left two things happening
in the shop that QuickBooks never heard about — units going out of the door, and
the money they went out for.

## Two writes, and how they hand over

**A printed line takes its own units out of stock.** One QuickBooks Purchase
carrying two lines that cancel each other out:

* an item line for the product's own QuickBooks item with a **negative**
  quantity — quantity on hand falls, Inventory Asset is credited;
* an account line for the same amount against the Cost of Goods Sold account
  chosen in Settings — the value of those units lands where a sold unit's value
  belongs.

The document totals zero, so the payment account it names is untouched. That
matters: a Purchase with only the negative line would total *below* zero, which
QuickBooks reads as money arriving in a bank account, and nothing arrived.

**An invoice records the sale, line by line, against the items actually sold.**
Each line names its own QuickBooks item — the product's, or the variation's
where it has one — so QuickBooks can report sales and cost of goods sold by
item, which an invoice of identical "Etsy sales" lines cannot.

**Which means the invoice moves stock too**, for every line billed on an
inventory item: QuickBooks relieves quantity on hand and books cost from the
invoice itself. That would be the same units twice. So raising an invoice
**takes back** the printed line's Purchase for exactly those lines — the
removal at print time is provisional, and the invoice is the real document. A
line billed on a service item (a bundle, or a product QuickBooks does not
track) keeps its removal, because nothing else is going to make it.

Net: one deduction per unit, whichever way the order goes. That hand-over is
the whole design, and changing either half without the other is how books stop
balancing.

## Rules that exist because this writes to real books

* Every write is once-only, and the proof is a column rather than a state.
  Line states here are *derived* — recomputed from scratch on every call (see
  services/state.py) — so "this line is printed" is true again on the next
  recompute and again on the one after. `qbo_stock_removed_at` is what actually
  stops the same units being booked twice.
* A write that fails is recorded on the row and shown, never swallowed. Somebody
  has to know that the removal did not happen.
* Removals can be taken back. Cancelling a line that was already booked deletes
  its Purchase, because stock that never left the shop should not stay gone.
* Nothing here writes without a stable idempotency key, so a timeout followed by
  a retry cannot produce a second document.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..integrations import qbo as qbo_api
from ..integrations.base import IntegrationError
from ..models import (
    JOB_DONE,
    LINE_CANCELLED,
    LINE_PRINTED,
    Order,
    OrderLine,
)
from . import audit, variations
from .credentials import IntegrationNotConfigured
from .finance import amount_of
from .manufacturing import money
from .settings_store import get_books_settings, get_manufacturing_settings

log = logging.getLogger("printflow.books")

# A fixed namespace, so the idempotency key for a given line or order is the
# same key tomorrow as it was during the request that timed out. Random keys
# would make QuickBooks' own replay protection useless, which is the one thing
# standing between a flaky network and a duplicate document.
NAMESPACE = uuid.UUID("6f1d7a2e-0f6b-4a1e-9e77-0b2b2c9a4d31")


class BooksError(RuntimeError):
    """A write cannot be made as things stand. The message is shown verbatim."""


def _key(*parts: str) -> str:
    return str(uuid.uuid5(NAMESPACE, "|".join(parts)))


# --------------------------------------------------------------------------
# Taking printed units out of stock
# --------------------------------------------------------------------------


def option_item(line: OrderLine) -> str | None:
    """The item mapped to one of the options this buyer actually chose.

    A listing sold in two scales is two items on the books, and the scale the
    buyer picked is what says which. The mappings live on the product and are
    set by hand — nothing here derives them, because which of a shop's items a
    combination is sold as is a decision about their books.

    Read from the line's own chosen options rather than from a matched
    variation, so it works for a line no variation described. Where a buyer's
    choices match more than one mapping — a scale and a loadout both naming an
    item — the first as the operator ordered them wins.
    """
    if line.product is None or not line.product.option_items:
        return None
    chosen = {
        (variations.normalize(option.get("name")), variations.normalize(option.get("value")))
        for option in (line.variations or [])
        if isinstance(option, dict) and option.get("name") and option.get("value")
    }
    if not chosen:
        return None
    for rule in sorted(line.product.option_items, key=lambda row: row.position):
        if (
            variations.normalize(rule.option_name),
            variations.normalize(rule.option_value),
        ) in chosen:
            return str(rule.qbo_item_id)
    return None


def line_item_id(line: OrderLine) -> str | None:
    """Which QuickBooks item this line is sold as, and drawn down from.

    Most specific first, and every step of it is something a person set:

    1. the variation's own item — one exact combination, named deliberately;
    2. an item mapped to one of the options the buyer chose, which is how one
       listing sold in several scales becomes several items;
    3. the product's own item, for a listing that is one thing on the books.

    Shared by the invoice and by the stock removal on purpose. A unit billed as
    one item and drawn down from another would be two different mistakes that
    look like one.
    """
    if line.variation is not None and line.variation.qbo_item_id:
        return str(line.variation.qbo_item_id)
    mapped = option_item(line)
    if mapped:
        return mapped
    if line.product is not None and line.product.qbo_item_id:
        return str(line.product.qbo_item_id)
    return None


def marked_printed(line: OrderLine) -> bool:
    """Did a person say this line was printed?

    The override, not the derived state. A leaf line whose parent is not a
    bundle never rests in `printed` — it goes straight on to `ready` — so the
    state is the wrong thing to read here.
    """
    return line.override_state == LINE_PRINTED


def printing_finished(line: OrderLine) -> bool:
    """Did this line's plates all come off the machines?

    Read from the plates rather than the line's state for the same reason:
    states are derived and a finished print may already have rolled on to
    `ready`, `labeled` or `shipped` by the time anyone asks.
    """
    if line.qty_to_print <= 0:
        return False
    statuses = [job.status for job in line.print_jobs]
    return bool(statuses) and all(status == JOB_DONE for status in statuses)


def removal_blocked(line: OrderLine) -> str | None:
    """Why this line's units cannot come out of stock, or None if they can."""
    if line.state == LINE_CANCELLED:
        return "The line is cancelled."
    if line.product is None:
        return "The line is not matched to a product."
    if line.product.fulfillment == "bundle":
        # A bundle is a container. Its components carry the QuickBooks items and
        # each books its own removal, so booking the container as well would
        # take the same goods out twice.
        return "A bundle is booked through its components, not as itself."
    if not line_item_id(line):
        return f"{line.product.name} has no QuickBooks item."
    if line.quantity <= 0:
        return "The line has no quantity."
    return None


def build_removal(
    line: OrderLine,
    *,
    item_id: str,
    unit_cost: Decimal,
    settings: dict[str, Any],
    books: dict[str, Any],
    order_number: str,
    when: datetime,
) -> dict[str, Any]:
    """The exact Purchase body for one line's stock removal.

    Pure, so the shape of what lands in somebody's books can be tested without
    a QuickBooks company behind it.
    """
    quantity = int(line.quantity)
    value = money(unit_cost * quantity)
    name = line.product.name if line.product else "item"
    label = f"Sold {quantity} × {name} on order {order_number}"

    lines: list[dict[str, Any]] = [
        {
            "DetailType": "ItemBasedExpenseLineDetail",
            # Negative: this is stock leaving. The amount follows the quantity,
            # so the document reads as a credit to Inventory Asset.
            "Amount": float(-value),
            "Description": label,
            "ItemBasedExpenseLineDetail": {
                "ItemRef": {"value": str(item_id)},
                "Qty": -quantity,
                "UnitPrice": float(money(unit_cost)),
                "BillableStatus": "NotBillable",
            },
        }
    ]
    # The other half. Without it the Purchase totals below zero, which reads as
    # money arriving in the payment account — and nothing arrived. With it the
    # document totals zero and the value lands in Cost of Goods Sold, which is
    # where the cost of a unit that has been sold belongs.
    if value != 0:
        lines.append(
            {
                "DetailType": "AccountBasedExpenseLineDetail",
                "Amount": float(value),
                "Description": f"Cost of goods sold — {label}",
                "AccountBasedExpenseLineDetail": {
                    "AccountRef": {"value": str(books["cogs_account_id"])}
                },
            }
        )

    body: dict[str, Any] = {
        "AccountRef": {"value": str(settings["account_id"])},
        "PaymentType": str(settings.get("payment_type") or "Cash"),
        "TxnDate": when.date().isoformat(),
        "PrivateNote": (
            f"PrintFlow — {label}. Stock removed when the line was printed; "
            "the invoice for this order records the sale and does not move stock."
        ),
        "Line": lines,
    }
    if settings.get("vendor_id"):
        body["EntityRef"] = {"value": str(settings["vendor_id"]), "type": "Vendor"}
    return body


async def _unit_cost(session: AsyncSession, item_id: str) -> Decimal:
    """What QuickBooks last recorded this item costing.

    Zero when it will not say. A removal at an unknown cost still moves the
    quantity, which is what was asked for; QuickBooks relieves inventory at its
    own average cost regardless, and a guessed figure here would be worse than
    an honest nothing.
    """
    try:
        client = await qbo_api.client_for(session)
        item = (await client.get_items([item_id])).get(str(item_id))
    except (IntegrationError, IntegrationNotConfigured) as exc:
        log.info("Could not read QuickBooks item %s: %s", item_id, exc)
        return Decimal("0")
    if not item:
        return Decimal("0")
    return qbo_api.item_purchase_cost(item) or Decimal("0")


async def remove_stock(
    session: AsyncSession, line: OrderLine, *, actor: str, order: Order | None = None
) -> dict[str, Any]:
    """Take this line's units out of QuickBooks stock. Once, ever.

    Returns a report rather than raising: this is called from the middle of
    other work — marking a line printed, a print finishing — and a line with no
    QuickBooks item must not fail the thing the operator actually asked for.

    `booked` says a Purchase was made. `failed` separates "tried and could not"
    — a missing setting, a QuickBooks refusal, something a person has to act on
    — from "there was never anything to book here", which is not news and is
    not worth telling anybody about on every recompute.
    """
    if line.qbo_stock_removed_at is not None:
        return {"booked": False, "failed": False, "reason": "Already out of stock."}

    blocked = removal_blocked(line)
    if blocked:
        return {"booked": False, "failed": False, "reason": blocked}

    books = await get_books_settings(session)
    if not books.get("cogs_account_id"):
        return {
            "booked": False,
            "failed": True,
            "reason": (
                "No cost of goods sold account is set. Choose one under "
                "Settings → QuickBooks books."
            ),
        }
    settings = await get_manufacturing_settings(session)
    if not settings.get("account_id"):
        return {
            "booked": False,
            "failed": True,
            "reason": (
                "No QuickBooks payment account is set. Choose one under "
                "Settings → Manufacturing."
            ),
        }

    if order is None:
        order = await session.get(Order, line.order_id)
    order_number = order.order_number if order else str(line.order_id)

    item_id = line_item_id(line)
    if item_id is None:  # removal_blocked already refused this; belt and braces
        return {"booked": False, "failed": False, "reason": "No QuickBooks item."}
    now = datetime.now(timezone.utc)
    body = build_removal(
        line,
        item_id=item_id,
        unit_cost=await _unit_cost(session, item_id),
        settings=settings,
        books=books,
        order_number=order_number,
        when=now,
    )

    try:
        client = await qbo_api.client_for(session)
        created = await client.create_purchase(
            body, request_id=_key("stock", str(line.id))
        )
    except (IntegrationError, IntegrationNotConfigured) as exc:
        # Recorded, not swallowed. If this was a timeout the Purchase may well
        # exist, and the idempotency key is what stops a retry duplicating it.
        line.qbo_stock_error = str(exc)
        await session.flush()
        return {"booked": False, "failed": True, "reason": str(exc)}

    line.qbo_stock_purchase_id = str(created.get("Id") or "") or None
    line.qbo_stock_sync_token = str(created.get("SyncToken") or "0")
    line.qbo_stock_qty = int(line.quantity)
    line.qbo_stock_removed_at = now
    line.qbo_stock_error = None
    await session.flush()

    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="qbo_stock_removed",
        detail={
            "order_number": order_number,
            "qbo_item_id": item_id,
            "quantity": int(line.quantity),
            "qbo_purchase_id": line.qbo_stock_purchase_id,
        },
        actor=actor,
    )
    return {
        "booked": True,
        "failed": False,
        "reason": None,
        "quantity": int(line.quantity),
        "qbo_purchase_id": line.qbo_stock_purchase_id,
    }


async def restore_stock(
    session: AsyncSession, line: OrderLine, *, actor: str
) -> dict[str, Any]:
    """Put back what a removal took out, by deleting its Purchase.

    For a line cancelled after its units were booked out. Stock that never left
    the shop should not stay gone, and QuickBooks has no reversal for a Purchase
    other than deleting it — which is exactly what voiding a made sheet does.
    """
    if line.qbo_stock_removed_at is None or not line.qbo_stock_purchase_id:
        return {"restored": False, "reason": "Nothing was taken out of stock."}

    purchase_id = line.qbo_stock_purchase_id
    try:
        client = await qbo_api.client_for(session)
        await client.void_purchase(purchase_id, line.qbo_stock_sync_token or "0")
    except (IntegrationError, IntegrationNotConfigured) as exc:
        line.qbo_stock_error = f"Could not put the stock back: {exc}"
        await session.flush()
        return {"restored": False, "reason": str(exc)}

    quantity = line.qbo_stock_qty
    line.qbo_stock_removed_at = None
    line.qbo_stock_purchase_id = None
    line.qbo_stock_sync_token = None
    line.qbo_stock_qty = None
    line.qbo_stock_error = None
    await session.flush()

    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="qbo_stock_restored",
        detail={"qbo_purchase_id": purchase_id, "quantity": quantity},
        actor=actor,
    )
    return {"restored": True, "quantity": quantity}


async def sync_order_stock(
    session: AsyncSession, order: Order, *, actor: str
) -> list[dict[str, Any]]:
    """Book what this order's finished lines owe the books, and undo what changed.

    Called after a recompute, which is the moment anything might have moved.
    Two directions, because both happen:

    * a line whose print has finished, or that somebody marked printed, and
      which has not been booked — book it;
    * a line that was booked and has since been cancelled — put it back.

    Finished prints only book on their own while the setting says so. Marking a
    line printed by hand always books, because that is a person asking for it.
    """
    books = await get_books_settings(session)
    automatic = bool(books.get("remove_stock_on_printed"))

    lines = (
        (
            await session.execute(
                select(OrderLine)
                .where(OrderLine.order_id == order.id)
                .options(selectinload(OrderLine.print_jobs))
            )
        )
        .scalars()
        .all()
    )

    results: list[dict[str, Any]] = []
    for line in lines:
        if line.qbo_stock_removed_at is not None:
            if line.state == LINE_CANCELLED:
                outcome = await restore_stock(session, line, actor=actor)
                results.append({"line_id": str(line.id), **outcome})
            continue
        if marked_printed(line) or (automatic and printing_finished(line)):
            outcome = await remove_stock(session, line, actor=actor, order=order)
            # Only what somebody needs to hear. "This line has no QuickBooks
            # item" is true on every recompute for the same lines and is
            # already visible on the line itself.
            if outcome["booked"] or outcome["failed"]:
                results.append({"line_id": str(line.id), **outcome})
    return results


# --------------------------------------------------------------------------
# Invoicing an order
# --------------------------------------------------------------------------


def transactions_of(order: Order) -> dict[int, dict[str, Any]]:
    """Etsy's transactions from the stored receipt, keyed by transaction id."""
    raw = order.raw or {}
    out: dict[int, dict[str, Any]] = {}
    for transaction in raw.get("transactions") or []:
        if not isinstance(transaction, dict):
            continue
        try:
            out[int(transaction["transaction_id"])] = transaction
        except (KeyError, TypeError, ValueError):
            continue
    return out


def unit_price(transaction: dict[str, Any]) -> Decimal | None:
    """What one of these cost the buyer.

    Etsy's `price` is per unit, not per line — the quantity is stated beside it
    — so this is the number that goes on an invoice line with the quantity.
    """
    return amount_of(transaction.get("price"))


def line_description(line: OrderLine) -> str:
    """What the buyer sees on the invoice line.

    The Etsy title first, because that is what they ordered and what they will
    recognise. The options they chose come after it: two invoice lines reading
    "Storage bin" with no colour between them is an invoice somebody has to
    open the order to understand.
    """
    name = (line.title or "").strip()
    if not name and line.product is not None:
        name = line.product.name
    chosen = [
        f"{option.get('name')}: {option.get('value')}"
        for option in (line.variations or [])
        if isinstance(option, dict) and option.get("name") and option.get("value")
    ]
    return f"{name} ({', '.join(chosen)})" if chosen else (name or "Item")


def customer_payload(order: Order) -> dict[str, Any]:
    """A QuickBooks customer built from what the order already knows.

    The buyer's name and the address they gave — the same address the parcel is
    going to, which is the only one Etsy provides. No email unless Etsy sent
    one: a customer record with a made-up contact is worse than a sparse one.
    """
    ship_to = order.ship_to or {}
    name = (order.buyer_name or "").strip() or (ship_to.get("name") or "").strip()
    display = name or f"Etsy buyer — order {order.order_number}"

    payload: dict[str, Any] = {"DisplayName": display[:100]}
    if name:
        parts = name.split()
        payload["GivenName"] = parts[0][:100]
        if len(parts) > 1:
            payload["FamilyName"] = " ".join(parts[1:])[:100]

    address = {
        key: str(ship_to[source])[:255]
        for key, source in (
            ("Line1", "first_line"),
            ("Line2", "second_line"),
            ("City", "city"),
            ("CountrySubDivisionCode", "state"),
            ("PostalCode", "zip"),
            ("Country", "country"),
        )
        if ship_to.get(source)
    }
    if address:
        payload["BillAddr"] = address
        payload["ShipAddr"] = dict(address)
    if ship_to.get("email"):
        payload["PrimaryEmailAddr"] = {"Address": str(ship_to["email"])[:100]}
    return payload


def invoice_item_for(line: OrderLine, books: dict[str, Any]) -> str | None:
    """Which QuickBooks item this invoice line should name.

    The thing that was actually sold: the line's own item, the variation's
    where it has one. An invoice reading "Storage bin" against the Storage bin
    item is one QuickBooks can report on — sales by item, cost of goods sold by
    item — and an invoice of identical "Etsy sales" lines is not.

    The fallback item from Settings covers what has no item of its own: a
    bundle, or a product QuickBooks does not track. Without it those lines have
    nothing to name, which is an error rather than a line quietly dropped.
    """
    return line_item_id(line) or (
        str(books["income_item_id"]) if books.get("income_item_id") else None
    )


def build_invoice(
    order: Order,
    lines: list[tuple[OrderLine, int, Decimal]],
    *,
    customer_id: str,
    books: dict[str, Any],
    shipping: Decimal | None,
    item_for: dict[Any, str],
) -> dict[str, Any]:
    """The exact Invoice body sent to QuickBooks. Pure.

    Each line names its own item, so the invoice is a record of what was sold
    rather than of how much money arrived. Where that item is an inventory
    item, QuickBooks relieves its stock and books its cost from this document —
    which is why invoicing takes back the provisional removal the printed line
    made. See `invoice_order`.
    """
    body_lines: list[dict[str, Any]] = []

    for line, quantity, price in lines:
        body_lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": float(money(price * quantity)),
                "Description": line_description(line),
                "SalesItemLineDetail": {
                    "ItemRef": {"value": str(item_for[line.id])},
                    "Qty": quantity,
                    "UnitPrice": float(money(price)),
                },
            }
        )

    # Postage the buyer paid, when Settings names an item to put it on. No item
    # means no line: a shop whose postage is accounted for elsewhere — or is
    # simply not worth a line — should not have one invented for it.
    if shipping and shipping > 0 and books.get("shipping_item_id"):
        body_lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": float(money(shipping)),
                "Description": "Shipping",
                "SalesItemLineDetail": {
                    "ItemRef": {"value": str(books["shipping_item_id"])},
                    "Qty": 1,
                    "UnitPrice": float(money(shipping)),
                },
            }
        )

    body: dict[str, Any] = {
        "CustomerRef": {"value": str(customer_id)},
        "Line": body_lines,
        "PrivateNote": f"PrintFlow — Etsy order {order.order_number}.",
    }
    if order.placed_at:
        body["TxnDate"] = order.placed_at.date().isoformat()
    if order.currency:
        body["CurrencyRef"] = {"value": str(order.currency)}
    return body


def invoice_lines(order: Order, lines: list[OrderLine]) -> list[tuple[OrderLine, int, Decimal]]:
    """The order's sellable lines with the price the buyer paid for each.

    Top-level lines only: the buyer bought a bundle, not its components, and an
    invoice that itemised the BOM would bill them for parts they never chose.

    A line whose price cannot be found is left out rather than billed at zero —
    a zero on an invoice looks like a decision somebody made.
    """
    transactions = transactions_of(order)
    out: list[tuple[OrderLine, int, Decimal]] = []
    for line in lines:
        if line.parent_line_id is not None or line.state == LINE_CANCELLED:
            continue
        transaction = transactions.get(int(line.etsy_transaction_id or 0))
        price = unit_price(transaction) if transaction else None
        if price is None:
            continue
        out.append((line, int(line.quantity), price))
    return out


async def find_or_create_customer(
    session: AsyncSession, client: qbo_api.QboClient, order: Order
) -> dict[str, Any]:
    """The QuickBooks customer for this order's buyer, made if need be.

    Matched on display name, which QuickBooks keeps unique. Two buyers who
    genuinely share a name will share a customer record — which is the lesser
    of the two wrongs, the other being a second customer QuickBooks refuses to
    create because the name is taken.
    """
    payload = customer_payload(order)
    existing = await client.find_customer(payload["DisplayName"])
    if existing:
        return existing
    return await client.create_customer(payload)


async def invoice_order(
    session: AsyncSession, order: Order, *, actor: str
) -> dict[str, Any]:
    """Raise the QuickBooks invoice for an order. Once per order.

    Each line names its own QuickBooks item. Where that item is an inventory
    item the invoice relieves its stock and books its cost — so the provisional
    removal the printed line made is taken back in the same breath, leaving one
    deduction per unit. Lines billed on an item that carries no stock keep their
    removal, because nothing else is going to make it.

    Raises BooksError with a sentence worth showing when it cannot be done. A
    person pressed a button for this, so nothing here fails quietly.
    """
    if order.qbo_invoice_id:
        raise BooksError(
            f"This order is already invoiced in QuickBooks "
            f"({order.qbo_invoice_doc_number or order.qbo_invoice_id})."
        )

    books = await get_books_settings(session)

    lines = (
        (
            await session.execute(
                select(OrderLine)
                .where(OrderLine.order_id == order.id)
                .options(selectinload(OrderLine.product))
                .order_by(OrderLine.created_at)
            )
        )
        .scalars()
        .all()
    )
    billable = invoice_lines(order, list(lines))
    if not billable:
        raise BooksError(
            "Nothing on this order has a price from Etsy, so there is nothing "
            "to invoice."
        )

    item_for = {line.id: invoice_item_for(line, books) for line, _, _ in billable}
    nameless = [line for line, _, _ in billable if not item_for[line.id]]
    if nameless:
        # Named by SKU as well as title, because the title is an Etsy listing
        # name — long, punctuated, and not what the Products search matches on.
        # Whoever reads this has to go and find these products, so the message
        # is written to be acted on rather than only to be correct.
        which = "; ".join(
            f"{(line.product.name if line.product else line.title) or 'an unmatched line'}"
            + (f" ({line.product.sku})" if line.product else "")
            for line in nameless
        )
        one = len(nameless) == 1
        raise BooksError(
            f"{'This product has' if one else 'These products have'} no "
            f"QuickBooks item, and no fallback is set for lines like "
            f"{'it' if one else 'them'}: {which}. Open "
            f"{'it' if one else 'them'} on the Products tab and link "
            f"{'an item' if one else 'items'}, or set one fallback item for "
            "everything under Settings → QuickBooks → Orders in the books."
        )

    try:
        client = await qbo_api.client_for(session)
        customer = await find_or_create_customer(session, client, order)
        customer_id = str(customer.get("Id") or "")
        if not customer_id:
            raise BooksError("QuickBooks did not return a customer to bill.")
        # Which of these lines the invoice will move stock for. Asked before
        # the write, because the answer decides what has to be undone after it
        # and a failure to read it afterwards would leave the books doubled.
        catalogue = await client.get_items(
            [str(item) for item in item_for.values() if item]
        )
        body = build_invoice(
            order,
            billable,
            customer_id=customer_id,
            books=books,
            shipping=order.shipping_total,
            item_for={key: str(value) for key, value in item_for.items() if value},
        )
        created = await client.create_invoice(
            body, request_id=_key("invoice", str(order.id))
        )
    except (IntegrationError, IntegrationNotConfigured) as exc:
        order.qbo_invoice_error = str(exc)
        await session.flush()
        raise BooksError(
            f"QuickBooks would not take the invoice: {exc}. Nothing was saved "
            "as invoiced — check QuickBooks before trying again."
        ) from exc

    order.qbo_customer_id = customer_id
    order.qbo_invoice_id = str(created.get("Id") or "") or None
    order.qbo_invoice_doc_number = str(created.get("DocNumber") or "") or None
    order.qbo_invoice_total = money(created.get("TotalAmt") or 0)
    order.qbo_invoice_at = datetime.now(timezone.utc)
    order.qbo_invoice_error = None
    await session.flush()

    # The invoice has just relieved stock for every line billed on an inventory
    # item. Those units were already taken out when the line was printed, so
    # that earlier Purchase is now a second deduction of the same goods — take
    # it back. A line billed on a service item keeps its removal, because
    # nothing else is going to make it.
    handed_over: list[dict[str, Any]] = []
    for line, _, _ in billable:
        item = catalogue.get(str(item_for[line.id]))
        if not (item and qbo_api.item_moves_stock(item)):
            continue
        if line.qbo_stock_removed_at is None:
            continue
        outcome = await restore_stock(session, line, actor=actor)
        handed_over.append({"line_id": str(line.id), **outcome})

    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="qbo_invoice_created",
        detail={
            "order_number": order.order_number,
            "qbo_invoice_id": order.qbo_invoice_id,
            "doc_number": order.qbo_invoice_doc_number,
            "customer_id": customer_id,
            "lines": len(body["Line"]),
            "total": str(order.qbo_invoice_total),
            # What the invoice took over from the printed lines. Worth logging:
            # it is the only record that a Purchase was deleted on purpose.
            "stock_handed_over": len([row for row in handed_over if row.get("restored")]),
        },
        actor=actor,
    )
    return {
        "qbo_invoice_id": order.qbo_invoice_id,
        "doc_number": order.qbo_invoice_doc_number,
        "total": str(order.qbo_invoice_total),
        "customer_id": customer_id,
        "stock_handed_over": handed_over,
    }


async def void_invoice(
    session: AsyncSession, order: Order, *, actor: str
) -> dict[str, Any]:
    """Void this order's invoice, freeing the order to be invoiced again."""
    if not order.qbo_invoice_id:
        raise BooksError("This order has no QuickBooks invoice.")

    invoice_id = order.qbo_invoice_id
    try:
        client = await qbo_api.client_for(session)
        current = await client.get_invoice(invoice_id)
        token = str((current or {}).get("SyncToken") or "0")
        await client.void_invoice(invoice_id, token)
    except (IntegrationError, IntegrationNotConfigured) as exc:
        order.qbo_invoice_error = str(exc)
        await session.flush()
        raise BooksError(f"QuickBooks would not void the invoice: {exc}") from exc

    doc_number = order.qbo_invoice_doc_number
    order.qbo_invoice_id = None
    order.qbo_invoice_doc_number = None
    order.qbo_invoice_total = None
    order.qbo_invoice_at = None
    order.qbo_invoice_error = None
    await session.flush()

    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="qbo_invoice_voided",
        detail={"qbo_invoice_id": invoice_id, "doc_number": doc_number},
        actor=actor,
    )
    return {"voided": True, "qbo_invoice_id": invoice_id, "doc_number": doc_number}
