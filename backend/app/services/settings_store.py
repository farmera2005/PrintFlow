"""Non-credential configuration, stored in the database (never in env vars)."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppSetting

log = logging.getLogger("printflow.settings")

KEY_SETUP_COMPLETE = "setup_complete"
KEY_POLL_INTERVALS = "poll_intervals"
KEY_PUBLIC_BASE_URL = "public_base_url"
KEY_HTTPS_REDIRECT = "https_redirect"
KEY_MANUFACTURING = "manufacturing"
KEY_BOOKS = "books"
KEY_CALCULATOR = "calculator"
KEY_ORDER_FLAGS = "order_flags"

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
    # Where an Etsy discount lands. Optional: left unset, QuickBooks puts it
    # against the company's own default discount account, which is right for
    # most shops and is one fewer thing to choose.
    "discount_account_id": None,
    "discount_account_name": None,
    # What an order *costs*. Two bills, so two accounts: the carrier's postage
    # and Etsy's cut. Neither has a default — which expense account is right
    # depends on a chart of accounts nobody here has seen.
    "shipping_expense_account_id": None,
    "shipping_expense_account_name": None,
    "fee_expense_account_id": None,
    "fee_expense_account_name": None,
    # Which account the money left. Unlike a stock removal — which totals zero
    # and never touches the account it names — these are real money going out,
    # so the account matters. Falls back to the manufacturing payment account,
    # because most shops clear all of this through the same one.
    "expense_payment_account_id": None,
    "expense_payment_account_name": None,
    # Whether finishing a print books the stock removal on its own. Off means
    # only a person pressing Mark printed moves anything, which is the older
    # rule that nothing but a person writes to the books.
    "remove_stock_on_printed": True,
}

BOOKS_FIELDS = tuple(DEFAULT_BOOKS)

# What the shop charges itself for everything it prints, typed once.
#
# No defaults for the rates. A machine rate somebody did not choose would put a
# number in front of them that looks worked out and is not, and the whole point
# of the calculator is a figure they can defend. The filament list starts with
# the two spools every shop has, because a starting row explains the field
# better than placeholder text does — and both are theirs to edit or delete.
DEFAULT_CALCULATOR: dict[str, Any] = {
    # Per hour the printer runs: the machine's own price over its life, plus
    # nozzles, belts and the afternoon spent fixing it.
    "machine_per_hour": "",
    "printer_watts": "",
    "electricity_per_kwh": "",
    "labour_per_hour": "",
    # Some prints fail, and the ones that work pay for the ones that did not.
    "failure_percent": "",
    "markup_percent": "",
    "filaments": [
        {"name": "PLA", "price_per_kg": ""},
        {"name": "PETG", "price_per_kg": ""},
    ],
}

CALCULATOR_FIELDS = tuple(DEFAULT_CALCULATOR)

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
    "wix_minutes": 5,
    "bambuddy_minutes": 2,
    "shipstation_minutes": 10,
    # Parcels do not move on a five-minute timer. This is how often the poll
    # wakes up at all; how often any one parcel is actually asked about is
    # decided per parcel, and widens as its journey goes on.
    "tracking_minutes": 30,
}

# The colours a flag may be, by name rather than by value.
#
# A name and not a hex code, because the screen turns these into Tailwind
# classes and Tailwind only builds the classes it can see written down. A
# colour typed into a settings box would arrive as a class nobody generated
# and draw as nothing at all — a flag that silently has no colour is worse
# than a short list of colours that work.
#
# Seven, which is as many as anybody can tell apart on a card at a glance,
# and each one separable from the others for a colour-blind reader by its
# place in this list as much as by its hue.
FLAG_COLORS = (
    "slate",
    "red",
    "amber",
    "emerald",
    "sky",
    "violet",
    "pink",
)
DEFAULT_FLAG_COLOR = "slate"

# A shop's flags are its own, so these are a starting point and not a
# fixture: every one of them can be renamed, recoloured or deleted. They
# exist because an empty list looks like a broken feature, and because these
# four are what a print shop writes on a sticky note most often.
DEFAULT_ORDER_FLAGS: list[dict[str, Any]] = [
    {"id": "rush", "label": "Rush", "color": "red"},
    {"id": "gift", "label": "Gift", "color": "violet"},
    {"id": "fragile", "label": "Fragile", "color": "amber"},
    {"id": "waiting-on-buyer", "label": "Waiting on buyer", "color": "sky"},
]

# What a flag can hold. A label long enough to say what it means and short
# enough to read on a card at arm's length; a list long enough for a shop's
# vocabulary and short enough that the pills do not become the card.
FLAG_LABEL_MAX = 24
FLAG_LIMIT = 20

POLL_INTERVAL_BOUNDS: dict[str, tuple[int, int]] = {
    "etsy_minutes": (1, 240),
    "wix_minutes": (1, 240),
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
    KEY_CALCULATOR: DEFAULT_CALCULATOR,
    KEY_ORDER_FLAGS: DEFAULT_ORDER_FLAGS,
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


async def get_calculator_settings(session: AsyncSession) -> dict[str, Any]:
    stored = await get_setting(session, KEY_CALCULATOR) or {}
    merged = dict(DEFAULT_CALCULATOR)
    if isinstance(stored, dict):
        for key in CALCULATOR_FIELDS:
            if key in stored:
                merged[key] = stored[key]
    if not isinstance(merged.get("filaments"), list):
        merged["filaments"] = list(DEFAULT_CALCULATOR["filaments"])
    return merged


async def set_calculator_settings(
    session: AsyncSession, values: dict[str, Any]
) -> dict[str, Any]:
    current = await get_calculator_settings(session)
    for key in CALCULATOR_FIELDS:
        if key in values:
            current[key] = values[key]
    if not isinstance(current.get("filaments"), list):
        current["filaments"] = []
    await set_setting(session, KEY_CALCULATOR, current)
    return current


def flag_id_for(label: str, taken: set[str]) -> str:
    """A stable id for a new flag, from what somebody called it.

    Readable, because these ids end up stored on orders and read in a backup
    — `rush` tells somebody what they are looking at where `f7c21a` does not.
    Minted once and never re-derived: the label is free to change afterwards
    without the orders carrying it noticing, which is the whole reason the
    order stores an id and not a word.
    """
    slug = "".join(
        char if char.isalnum() else "-" for char in label.strip().lower()
    ).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug[:32] or "flag"
    if slug not in taken:
        return slug
    # Two flags called the same thing is a shop's business, but two flags
    # with the same id is a bug: the second would silently become the first.
    for suffix in range(2, 1000):
        candidate = f"{slug}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError("Too many flags with that name.")


def clean_order_flags(values: Any) -> list[dict[str, Any]]:
    """Whatever arrived, as a list of flags this shop can actually use.

    Raises rather than quietly dropping a bad one: a flag that vanished on
    save, having been typed and seen, is the sort of thing somebody only
    notices when the order it was meant for has already shipped.
    """
    if not isinstance(values, list):
        raise ValueError("Flags must be a list.")
    if len(values) > FLAG_LIMIT:
        raise ValueError(f"That is more than {FLAG_LIMIT} flags.")

    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for entry in values:
        if not isinstance(entry, dict):
            raise ValueError("Each flag needs a label and a colour.")
        label = str(entry.get("label") or "").strip()
        if not label:
            raise ValueError("A flag needs a name.")
        if len(label) > FLAG_LABEL_MAX:
            raise ValueError(
                f"“{label[:FLAG_LABEL_MAX]}…” is longer than {FLAG_LABEL_MAX} "
                "characters, which will not read on a card."
            )
        color = str(entry.get("color") or "").strip().lower()
        if color not in FLAG_COLORS:
            color = DEFAULT_FLAG_COLOR
        # An id that came back from the screen is kept, because orders are
        # carrying it. One that did not is a flag being invented right now.
        flag_id = str(entry.get("id") or "").strip().lower()
        if not flag_id or flag_id in taken:
            flag_id = flag_id_for(label, taken)
        taken.add(flag_id)
        out.append({"id": flag_id, "label": label, "color": color})
    return out


async def get_order_flags(session: AsyncSession) -> list[dict[str, Any]]:
    """The flags this shop has, in the order it put them in.

    Order matters: it is the order they are offered in and drawn in, and a
    shop that puts its most-used flag first should see it first everywhere.
    """
    # Absent means a new shop, and `get_setting` hands back the defaults for
    # that. Present but unreadable is a different thing entirely, and the
    # answer to it is nothing rather than the defaults: a shop that deleted
    # three of the four and then hit corruption should not find them back
    # without being told, which is how a setting quietly undoes a decision.
    stored = await get_setting(session, KEY_ORDER_FLAGS)
    try:
        return clean_order_flags(stored)
    except ValueError as exc:
        # Falling over here would take the whole board down with it, and the
        # board matters a great deal more than the flags do.
        log.warning("Stored order flags are unreadable (%s); offering none", exc)
        return []


async def set_order_flags(
    session: AsyncSession, values: Any
) -> list[dict[str, Any]]:
    cleaned = clean_order_flags(values)
    await set_setting(session, KEY_ORDER_FLAGS, cleaned)
    return cleaned


async def is_setup_complete(session: AsyncSession) -> bool:
    return bool(await get_setting(session, KEY_SETUP_COMPLETE))


async def has_admin_user(session: AsyncSession) -> bool:
    from ..models import User

    return (await session.execute(select(User.id).limit(1))).first() is not None
