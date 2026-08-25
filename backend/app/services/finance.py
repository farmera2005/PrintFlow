"""What an order was worth, where it went, and what Etsy took for it.

Three quite different things, from three places, which is why they are read
separately rather than in one pass:

* **The address and the money the buyer paid** are already in the receipt
  PrintFlow stores on every order. They cost nothing to read and are available
  the instant an order arrives.
* **The fees** are not in the receipt and cannot be: at the moment a receipt
  exists Etsy has not charged anything yet. They land afterwards on the shop's
  payment ledger, sometimes days later, which is why this is a sweep that runs
  again rather than something done once at intake.
* **The label** is PrintFlow's own, bought through ShipStation, and is already
  on the order.

Together they answer the question a shop actually asks — *what did this order
make* — which none of the three systems can answer on its own.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..integrations import etsy as etsy_api
from ..integrations import wix
from ..integrations.base import IntegrationError
from ..models import SOURCE_WIX, Order
from ..services.manufacturing import money

log = logging.getLogger("printflow.finance")

# How far back a fee sweep looks. Etsy posts most fees within a day or two of
# the sale, and a deposit's ledger entries can trail the order by longer; a
# month and a half covers both without paging through a year of history.
FEE_WINDOW_DAYS = 45

# Which fees are which. Matched against the ledger's own words, longest and
# most specific first, because "Etsy Ads" and "Etsy fee" both start with Etsy.
MARKETING_WORDS = (
    "offsite ad", "offsite_ad", "etsy ads", "etsy_ads", "advertis", "marketing",
    "promoted",
)
PROCESSING_WORDS = ("processing", "payment fee", "card fee")
FEE_WORDS = ("fee", "commission", "tax on fee")


def amount_of(value: Any) -> Decimal | None:
    """One Etsy money object as an amount.

    Etsy sends money as an integer and the divisor to apply — 1234 over 100 —
    which is exact where a float would not be. Anything else (a bare number, a
    string) is accepted too, since the same shape is not used everywhere.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        raw, divisor = value.get("amount"), value.get("divisor")
        if raw is None:
            return None
        try:
            return Decimal(str(raw)) / Decimal(str(divisor or 100))
        except (InvalidOperation, ArithmeticError, ValueError):
            return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def currency_of(receipt: dict[str, Any]) -> str | None:
    for key in ("grandtotal", "total_price", "subtotal"):
        value = receipt.get(key)
        if isinstance(value, dict) and value.get("currency_code"):
            return str(value["currency_code"])
    return None


def address_from(receipt: dict[str, Any]) -> dict[str, Any] | None:
    """Where the parcel goes, as Etsy states it.

    Etsy also sends `formatted_address`, already laid out for the destination
    country — which is the version to put on a label, since address order is
    not the same everywhere. The parts are kept alongside it because a search
    for "everything going to Illinois" needs a field, not a paragraph.
    """
    parts = {
        "name": receipt.get("name"),
        "first_line": receipt.get("first_line"),
        "second_line": receipt.get("second_line"),
        "city": receipt.get("city"),
        "state": receipt.get("state"),
        "zip": receipt.get("zip"),
        "country": receipt.get("country_iso"),
        "formatted": receipt.get("formatted_address"),
        "email": receipt.get("buyer_email"),
    }
    kept = {k: str(v).strip() for k, v in parts.items() if isinstance(v, str) and v.strip()}
    return kept or None


def totals_from(receipt: dict[str, Any]) -> dict[str, Decimal | str | None]:
    """What the buyer paid, split the way Etsy splits it.

    `grandtotal` is the whole of it — items, shipping and tax together, less
    any discount — and is what "revenue" means here. The parts are kept because
    shipping the buyer paid for is not the same kind of money as the item
    price, and a shop that wants to know whether its postage is covered needs
    them apart.
    """
    return {
        "currency": currency_of(receipt),
        "revenue": amount_of(receipt.get("grandtotal")),
        "items_total": amount_of(receipt.get("total_price")),
        "shipping_total": amount_of(receipt.get("total_shipping_cost")),
        # VAT and sales tax are separate fields and only one is ever set.
        "tax_total": amount_of(receipt.get("total_tax_cost"))
        or amount_of(receipt.get("total_vat_cost")),
        "discount_total": amount_of(receipt.get("discount_amt")),
    }


def classify_fee(entry: dict[str, Any]) -> str | None:
    """Which bucket a ledger line belongs in, by what the ledger calls it.

    Etsy does not label a line "marketing"; it says "Offsite Ads fee for order
    …". Reading the words is the only way, so the words are listed rather than
    hidden in a regex, and anything that is not recognisably a fee is left out
    entirely — a deposit or a refund is not a cost of selling.
    """
    words = " ".join(
        str(entry.get(key) or "")
        for key in ("description", "entry_type", "reference_type")
    ).lower()
    if any(word in words for word in MARKETING_WORDS):
        return "marketing"
    if any(word in words for word in PROCESSING_WORDS):
        return "processing"
    if any(word in words for word in FEE_WORDS):
        return "etsy"
    return None


def _references(receipt: dict[str, Any], payments: list[dict[str, Any]]) -> set[str]:
    """Every id a fee for this order might be filed under.

    A ledger line points at whatever caused it, and for one sale that can be
    the receipt, the payment that settled it, or an individual transaction.
    None of the three is reliably the one used, so all of them are collected
    and a line matching any is this order's.
    """
    ids: set[str] = set()
    for key in ("receipt_id", "order_id"):
        if receipt.get(key) is not None:
            ids.add(str(receipt[key]))
    for transaction in receipt.get("transactions") or []:
        if isinstance(transaction, dict) and transaction.get("transaction_id") is not None:
            ids.add(str(transaction["transaction_id"]))
    for payment in payments:
        for key in ("payment_id", "receipt_id", "shop_payment_id"):
            if payment.get(key) is not None:
                ids.add(str(payment[key]))
    return ids


def fees_from(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Total the fee lines, and keep them.

    The ledger states money leaving as a negative amount. Fees are stored as
    the positive size of the bite so that a column of them can be read and
    subtracted; the sign is a direction, not part of the figure.
    """
    totals = {"etsy": Decimal(0), "marketing": Decimal(0), "processing": Decimal(0)}
    lines: list[dict[str, Any]] = []
    for entry in entries:
        kind = classify_fee(entry)
        amount = amount_of(entry.get("amount"))
        if kind is None or amount is None or amount == 0:
            continue
        size = abs(amount)
        totals[kind] += size
        lines.append(
            {
                "kind": kind,
                "description": entry.get("description"),
                "amount": str(money(size)),
                "ledger_entry_id": entry.get("ledger_entry_id") or entry.get("entry_id"),
                "created_at": entry.get("create_date"),
            }
        )
    return {
        "etsy_fees": totals["etsy"],
        "marketing_fees": totals["marketing"],
        "processing_fees": totals["processing"],
        "fee_lines": lines,
    }


MONEY_FIELDS = (
    "currency", "revenue", "items_total", "shipping_total", "tax_total",
    "discount_total",
)


def _snapshot(order: Order) -> tuple[Any, ...]:
    return (order.ship_to, *(getattr(order, field) for field in MONEY_FIELDS))


def apply_stored(order: Order) -> bool:
    """Read the address and the money out of the payload already stored.

    No API call: this is a payload PrintFlow has had since the order arrived.
    Every order gets this on every poll, which is what backfills the ones taken
    before any of it was being read.

    Which reader runs depends on where the order came from. The two channels
    describe the same six figures in different words, and this is the one place
    that has to know the difference — everything downstream reads the columns.
    """
    payload = order.raw or {}
    if not payload:
        return False
    before = _snapshot(order)
    if order.source == SOURCE_WIX:
        parsed = wix.parse_order(payload)
        address = parsed.get("ship_to")
        found = {field: parsed.get(field) for field in MONEY_FIELDS}
    else:
        address = address_from(payload)
        found = totals_from(payload)
    if address:
        order.ship_to = address
    for field, value in found.items():
        if value is not None:
            setattr(order, field, value)
    return before != _snapshot(order)


# The name intake and the fee sweep have always called it by. Kept because
# "apply the receipt" is still exactly what it does for an Etsy order.
apply_receipt = apply_stored


async def backfill(session: AsyncSession, *, limit: int = 2000) -> dict[str, int]:
    """Read the address and the money out of every receipt already stored.

    This is a database job, not an Etsy one: the payloads are already here. It
    exists because the obvious place to do the work — intake, as receipts come
    in — only ever sees receipts the poll fetches, and the poll asks for
    *unshipped* ones. Every order that had already shipped would therefore have
    waited for a re-fetch that never comes, which for an established shop is
    most of its history.
    """
    orders = (
        (
            await session.execute(
                select(Order)
                # Revenue is the marker rather than the address, because it is
                # a Numeric column: unambiguously NULL or not, whatever an
                # older release may have written into the JSON ones.
                .where(Order.raw.is_not(None), Order.revenue.is_(None))
                .order_by(Order.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    filled = sum(1 for order in orders if apply_stored(order))
    if filled:
        await session.flush()
    return {"looked_at": len(orders), "filled": filled}


def net_of(order: Order) -> Decimal | None:
    """What the order actually made: revenue, less every fee, less the label.

    None where revenue is unknown, because a net worked out from fees alone
    would be a negative number presented as a result.
    """
    if order.revenue is None:
        return None
    total = Decimal(str(order.revenue))
    for field in ("etsy_fees", "marketing_fees", "processing_fees", "label_cost"):
        value = getattr(order, field, None)
        if value is not None:
            total -= Decimal(str(value))
    return total


async def sync_fees(
    session: AsyncSession, *, limit: int = 200, days: int = FEE_WINDOW_DAYS
) -> dict[str, int]:
    """Sweep the shop's payment ledger and attribute the fees to orders.

    One sweep for the whole window rather than a lookup per order: the ledger
    is a single stream and paging it once for fifty orders is fifty times less
    work than asking about each of them. Orders older than the window are left
    alone — their fees settled long ago and will not change.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # When the sale happened, falling back to when PrintFlow first saw it: a
    # receipt without a timestamp is rare but it is still a sale, and an order
    # that can never be swept would sit without fees forever.
    when = func.coalesce(Order.placed_at, Order.created_at)
    orders = (
        (
            await session.execute(
                select(Order).where(when >= since).order_by(when.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    stats = {"orders": len(orders), "priced": 0, "fee_lines": 0}
    if not orders:
        return stats

    client = await etsy_api.client_for(session)
    shop_id = client.shop_id
    if shop_id is None:
        raise IntegrationError("etsy", "No Etsy shop is selected.")

    entries = await client.iter_ledger_entries(
        shop_id=shop_id, min_created=int(since.timestamp())
    )
    by_reference: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        reference = entry.get("reference_id")
        if reference is not None:
            by_reference.setdefault(str(reference), []).append(entry)

    now = datetime.now(timezone.utc)
    for order in orders:
        if order.fees_source == "manual":
            # Somebody typed these, and may already have expensed them. A sweep
            # quietly replacing a figure a person put there — and QuickBooks
            # has — is how the books and the screen stop agreeing. Clearing the
            # entry hands the order back to the sweep. Skipped here rather than
            # after the work, because none of the work below is wanted.
            stats["kept_by_hand"] = stats.get("kept_by_hand", 0) + 1
            continue

        receipt = order.raw or {}
        payments: list[dict[str, Any]] = []
        try:
            payments = await client.receipt_payments(
                shop_id=shop_id, receipt_id=order.etsy_receipt_id
            )
        except IntegrationError as exc:
            # A payment record that will not load is not a reason to abandon
            # the sweep: the ledger may still name the receipt directly.
            log.warning("Etsy payments unavailable for %s: %s", order.order_number, exc)

        mine = [
            entry
            for reference in _references(receipt, payments)
            for entry in by_reference.get(reference, [])
        ]
        seen: set[Any] = set()
        unique = []
        for entry in mine:
            key = entry.get("ledger_entry_id") or id(entry)
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)

        found = fees_from(unique)
        for field, value in found.items():
            setattr(order, field, value)
        order.finance_synced_at = now
        if found["fee_lines"]:
            order.fees_source = "etsy"
            stats["priced"] += 1
            stats["fee_lines"] += len(found["fee_lines"])

    await session.flush()
    return stats
