"""Non-credential configuration, stored in the database (never in env vars)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppSetting

KEY_SETUP_COMPLETE = "setup_complete"
KEY_POLL_INTERVALS = "poll_intervals"
KEY_PUBLIC_BASE_URL = "public_base_url"
KEY_HTTPS_REDIRECT = "https_redirect"
KEY_MANUFACTURING = "manufacturing"
KEY_BOOKS = "books"

# How a made-items sheet posts to QuickBooks.
#
# `account_id` is the Purchase's AccountRef — which QuickBooks defines as the
# account the money came *out of*, not where the cost lands. It must be a Bank
# account (or a Credit Card account when the payment type is CreditCard);
# anything else is rejected with "Invalid account type used" (error 6430).
#
# `offset_account_id` is optional and is where value added beyond the
# components goes — labour, machine time. Without it, that difference is
# credited to the payment account, which reads as money leaving a bank account
# that nothing actually left.
DEFAULT_MANUFACTURING: dict[str, Any] = {
    "account_id": None,
    "account_name": None,
    "offset_account_id": None,
    "offset_account_name": None,
    "payment_type": "Cash",
    "vendor_id": None,
    "vendor_name": None,
    "doc_number_prefix": "",
}

MANUFACTURING_FIELDS = tuple(DEFAULT_MANUFACTURING)
# QuickBooks rejects anything else on a Purchase.
PAYMENT_TYPES = ("Cash", "Check", "CreditCard")

# What the order pipeline writes into QuickBooks, and where it lands.
#
# * a printed line takes its own item out of stock — inventory asset down,
#   `cogs_account_id` up. That account is where the value of a sold unit goes,
#   so it wants to be a Cost of Goods Sold account.
# * an invoice bills each line against **its own** QuickBooks item, so sales
#   and cost of goods sold can be reported by item.
#
# `income_item_id` is the fallback for lines with no item of their own — a
# bundle, or a product QuickBooks does not track. It must be a **Service or
# Non-Inventory** item: it stands in for goods QuickBooks holds no stock of, so
# a line naming it must not pretend to move any.
#
# `shipping_item_id` is optional and means what it says: no item, no shipping
# line. A shop that accounts for postage elsewhere should not have a line
# invented for it.
#
# The Purchase behind a stock removal reuses the manufacturing payment account,
# because it is the same books and the document totals zero either way.
DEFAULT_BOOKS: dict[str, Any] = {
    "cogs_account_id": None,
    "cogs_account_name": None,
    "income_item_id": None,
    "income_item_name": None,
    "shipping_item_id": None,
    "shipping_item_name": None,
    # Whether finishing a print books the stock removal on its own. Off means
    # only a person pressing Mark printed moves anything, which is the older
    # rule that nothing but a person writes to the books.
    "remove_stock_on_printed": True,
}

BOOKS_FIELDS = tuple(DEFAULT_BOOKS)

# What `cogs_account_id` is allowed to be. QuickBooks will accept an expense
# account on the line, and a shop that books its printing to "Materials" rather
# than to Cost of Goods Sold is not wrong — so both are offered.
COGS_ACCOUNT_TYPES = ("Cost of Goods Sold", "Expense", "Other Expense")

# Item types the fallback and shipping settings may name. Enforced rather than
# advised: both stand in for things QuickBooks holds no stock of, and an
# Inventory item there would move stock nobody meant to move.
NON_STOCK_ITEM_TYPES = ("Service", "NonInventory", "Category")

# What AccountRef may be, per payment type. QuickBooks has no "Cash" account
# type — petty cash is a Bank account like any other.
PAYMENT_ACCOUNT_TYPES: dict[str, tuple[str, ...]] = {
    "Cash": ("Bank",),
    "Check": ("Bank",),
    "CreditCard": ("Credit Card",),
}

# Defaults from §6 of the build spec.
DEFAULT_POLL_INTERVALS: dict[str, int] = {
    "etsy_minutes": 5,
    "bambuddy_minutes": 2,
    "shipstation_minutes": 10,
    # Parcels do not move on a five-minute timer. This is how often the poll
    # wakes up at all; how often any one parcel is actually asked about is
    # decided per parcel, and widens as its journey goes on.
    "tracking_minutes": 30,
}

POLL_INTERVAL_BOUNDS: dict[str, tuple[int, int]] = {
    "etsy_minutes": (1, 240),
    "bambuddy_minutes": (1, 240),
    "shipstation_minutes": (1, 240),
    "tracking_minutes": (5, 1440),
}

_DEFAULTS: dict[str, Any] = {
    KEY_SETUP_COMPLETE: False,
    KEY_POLL_INTERVALS: DEFAULT_POLL_INTERVALS,
    KEY_PUBLIC_BASE_URL: None,
    # Off by default: a redirect that outlives a broken certificate would lock
    # the operator out of the only UI that can fix it.
    KEY_HTTPS_REDIRECT: False,
    KEY_MANUFACTURING: DEFAULT_MANUFACTURING,
    KEY_BOOKS: DEFAULT_BOOKS,
}


async def get_setting(session: AsyncSession, key: str) -> Any:
    row = await session.get(AppSetting, key)
    if row is None:
        return _DEFAULTS.get(key)
    # Values are stored wrapped so that scalars (bool/str) round-trip through JSON.
    return row.value.get("v") if isinstance(row.value, dict) and "v" in row.value else row.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(AppSetting, key)
    if row is None:
        session.add(AppSetting(key=key, value={"v": value}))
    else:
        row.value = {"v": value}
    await session.flush()


async def get_poll_intervals(session: AsyncSession) -> dict[str, int]:
    stored = await get_setting(session, KEY_POLL_INTERVALS) or {}
    merged = dict(DEFAULT_POLL_INTERVALS)
    if isinstance(stored, dict):
        for key, value in stored.items():
            if key in merged and isinstance(value, int):
                merged[key] = value
    return merged


def clamp_poll_intervals(values: dict[str, Any]) -> dict[str, int]:
    out = dict(DEFAULT_POLL_INTERVALS)
    for key, (low, high) in POLL_INTERVAL_BOUNDS.items():
        raw = values.get(key)
        if raw is None:
            continue
        try:
            out[key] = max(low, min(high, int(raw)))
        except (TypeError, ValueError):
            continue
    return out


async def get_manufacturing_settings(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, KEY_MANUFACTURING) or {}
    merged = dict(DEFAULT_MANUFACTURING)
    if isinstance(stored, dict):
        for key in MANUFACTURING_FIELDS:
            if key in stored:
                merged[key] = stored[key]
    if merged.get("payment_type") not in PAYMENT_TYPES:
        merged["payment_type"] = DEFAULT_MANUFACTURING["payment_type"]
    return merged


async def set_manufacturing_settings(
    session: AsyncSession, values: dict[str, Any]
) -> dict[str, Any]:
    current = await get_manufacturing_settings(session)
    for key in MANUFACTURING_FIELDS:
        if key in values:
            current[key] = values[key]
    if current.get("payment_type") not in PAYMENT_TYPES:
        current["payment_type"] = DEFAULT_MANUFACTURING["payment_type"]
    await set_setting(session, KEY_MANUFACTURING, current)
    return current


async def get_books_settings(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, KEY_BOOKS) or {}
    merged = dict(DEFAULT_BOOKS)
    if isinstance(stored, dict):
        for key in BOOKS_FIELDS:
            if key in stored:
                merged[key] = stored[key]
    merged["remove_stock_on_printed"] = bool(merged.get("remove_stock_on_printed"))
    return merged


async def set_books_settings(
    session: AsyncSession, values: dict[str, Any]
) -> dict[str, Any]:
    current = await get_books_settings(session)
    for key in BOOKS_FIELDS:
        if key in values:
            current[key] = values[key]
    current["remove_stock_on_printed"] = bool(current.get("remove_stock_on_printed"))
    await set_setting(session, KEY_BOOKS, current)
    return current


async def is_setup_complete(session: AsyncSession) -> bool:
    return bool(await get_setting(session, KEY_SETUP_COMPLETE))


async def has_admin_user(session: AsyncSession) -> bool:
    from ..models import User

    return (await session.execute(select(User.id).limit(1))).first() is not None
