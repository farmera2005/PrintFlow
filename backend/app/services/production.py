"""Everything the shop still has to make, and what is being made right now.

PrintFlow already knows this, scattered: the board knows which orders are
open, each order knows which of its lines are short, and the Printers tab
knows what is on a machine. What nobody could see was the shape a print shop
actually works to — *one thing, and how many of it*. Six orders each wanting
one bracket is six brackets, and finding that out meant opening six orders.

So this groups by **what is made rather than by who ordered it**: a row per
product and variation, because two variations of one product are made
differently and are two different things to make.

**One list, and one number per thing: how many of it are not in hand yet.**

Where a unit comes from is not a second list. Some are printed and some come
off a shelf, and a person holding this list still has to end the day with the
same count either way — so a thing an order needs is one row whatever is
going to satisfy it. The split is kept on the row, because walking to a shelf
and setting up a machine are different afternoons, but it is a note, not a
division.

The only thing that is set apart is work **already under way**: PrintFlow
started it without being asked, so it is not a job for a person; it is
something to watch finish. Everything else is the list.

A unit that is already in hand — printed, picked, packed — is on neither.
That is what makes the number worth acting on: it is what is missing, not
what an order adds up to.

How the making actually happens — which file, which machine, what was sent
where — is the Printers tab's business and deliberately not this one's. This
screen is a list of things and quantities.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    JOB_DONE,
    JOB_PENDING,
    JOB_PRINTING,
    JOB_QUEUED,
    LINE_CANCELLED,
    LINE_LABELED,
    LINE_PRINTED,
    LINE_READY,
    LINE_SHIPPED,
    LINE_UNMATCHED,
    BOARD_COLUMNS,
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_SHIPPED,
    Order,
    OrderLine,
    PrintFile,
    Product,
    ProductionPriority,
    ProductVariation,
)
from . import variations as variation_rules

log = logging.getLogger("printflow.production")

# Orders that have stopped asking for anything to be made. A shipped order's
# parts exist; a cancelled one's are not wanted.
CLOSED_ORDERS = (ORDER_SHIPPED, ORDER_COMPLETE, ORDER_CANCELLED)

# The board columns this screen can draw from, in the board's own order. The
# closed ones are not offered rather than offered and always empty: a filter
# that can only ever return nothing is a question the screen should not ask.
OPEN_COLUMNS = tuple(name for name in BOARD_COLUMNS if name not in CLOSED_ORDERS)

# Lines whose making is behind them, whatever the order is doing.
MADE_STATES = (LINE_PRINTED, LINE_READY, LINE_LABELED, LINE_SHIPPED, LINE_CANCELLED)

# A line that is out of the door, or was never going out of it. Its shelf
# units are picked by now: a stocked item stays on its shelf until somebody
# makes the parcel, which is the moment the line gets labelled.
GONE = (LINE_LABELED, LINE_SHIPPED, LINE_CANCELLED)

# Work that is under way: the machinery of it belongs to the Printers tab, so
# all this module takes from it is how many units are at each stage of being
# made. Anything that failed or was cancelled is not under way — which is
# exactly why those units come back onto the to-make list rather than sit
# somewhere looking finished.
UNDER_WAY = (JOB_PENDING, JOB_QUEUED, JOB_PRINTING, JOB_DONE)
# Started and finished, waiting for the rest of its line.
FINISHED = (JOB_DONE,)
# Happening on something, right now.
HAPPENING = (JOB_PRINTING,)


def _key(line: OrderLine) -> tuple[Any, Any]:
    """What makes two lines the same thing to make.

    The variation is part of it: "with fan" and "without fan" are made
    differently, and adding them together would produce a number that matches
    no single way of making them.
    """
    return (line.product_id, line.variation_id)


def _units(line: OrderLine, statuses: tuple[str, ...]) -> int:
    """Units of this line's work sitting at one stage of being made."""
    return sum(
        job.units_expected for job in line.print_jobs if job.status in statuses
    )


def _started_at(line: OrderLine) -> datetime | None:
    """When work on this line first began moving."""
    stamps = [
        job.queued_at or job.created_at
        for job in line.print_jobs
        if job.status in UNDER_WAY
    ]
    return min(stamps) if stamps else None


async def _makeable(
    session: AsyncSession, lines: list[OrderLine]
) -> dict[tuple[Any, Any], bool]:
    """Whether PrintFlow has any way to make each thing.

    The same question the planner asks before it works anything out, asked
    once for the whole list — because "nobody has started this" and "nothing
    here can start it" look identical on a screen and need completely
    different answers from a person.
    """
    product_ids = {line.product_id for line in lines if line.product_id}
    variation_ids = {line.variation_id for line in lines if line.variation_id}
    if not product_ids:
        return {}

    files: dict[Any, list[PrintFile]] = {}
    for row in (
        (
            await session.execute(
                select(PrintFile)
                .where(PrintFile.product_id.in_(product_ids))
                .order_by(PrintFile.created_at, PrintFile.id)
            )
        )
        .scalars()
        .all()
    ):
        files.setdefault(row.product_id, []).append(row)

    found_variations = {
        row.id: row
        for row in (
            (
                await session.execute(
                    select(ProductVariation).where(ProductVariation.id.in_(variation_ids))
                )
            )
            .scalars()
            .all()
        )
        if variation_ids
    }

    out: dict[tuple[Any, Any], bool] = {}
    for line in lines:
        key = _key(line)
        if key in out or not line.product_id:
            continue
        out[key] = bool(
            variation_rules.print_plans(
                files.get(line.product_id, []), found_variations.get(line.variation_id)
            )
        )
    return out


def _when(order: Order) -> datetime | None:
    return order.placed_at or order.created_at


def resolve_columns(columns: Sequence[str] | None) -> tuple[str, ...]:
    """Which board columns to draw from, from whatever the caller asked for.

    Nothing asked for means all of them, because the useful default is the
    whole shop. A name this screen cannot show is an error rather than a
    silent fallback to everything: a typo that quietly widens a filter is
    the sort of thing somebody only notices after acting on the wrong list.
    """
    if not columns:
        return OPEN_COLUMNS
    chosen: list[str] = []
    for name in columns:
        key = (name or "").strip()
        if not key:
            continue
        if key not in OPEN_COLUMNS:
            raise ValueError(
                f"{key!r} is not a column this list can show. "
                f"Open columns are: {', '.join(OPEN_COLUMNS)}."
            )
        if key not in chosen:
            chosen.append(key)
    return tuple(chosen) if chosen else OPEN_COLUMNS


async def to_make(
    session: AsyncSession, *, columns: Sequence[str] | None = None
) -> dict[str, Any]:
    """One list of things to make, what is already being made, and what is stuck.

    `columns` narrows it to orders sitting in those board columns — "what do
    the orders nobody has started on need", or "what is the assembly bench
    waiting for". The default is every open column, which is the whole shop.

    One query for the lines and their products — the shop floor refreshes
    this screen, and a query per row would make a list of forty things forty
    round trips. The narrowing happens here rather than in the query, because
    the column tallies have to count what the *unfiltered* shop holds: a chip
    whose number changed as you pressed it would be no way back.
    """
    chosen = resolve_columns(columns)
    everything = list(
        (
            await session.execute(
                select(OrderLine)
                .join(Order, OrderLine.order_id == Order.id)
                .where(Order.status.notin_(CLOSED_ORDERS))
                .options(
                    selectinload(OrderLine.print_jobs),
                    selectinload(OrderLine.product),
                    selectinload(OrderLine.variation),
                    selectinload(OrderLine.order),
                )
            )
        )
        .scalars()
        .all()
    )

    # A bundle is never a row: it is not a thing anybody makes, it is a thing
    # somebody puts together out of other rows. Its components are ordinary
    # lines with their own quantities, so they are already on the list — all
    # the bundle contributes is its name, as what its parts are for.
    children_of: dict[Any, list[OrderLine]] = {}
    for line in everything:
        if line.parent_line_id is not None:
            children_of.setdefault(line.parent_line_id, []).append(line)

    has_children = set(children_of)
    bundle_of: dict[Any, str] = {}
    for line in everything:
        kids = children_of.get(line.id)
        if not kids:
            continue
        name = line.product.name if line.product else (line.title or line.sku_raw)
        for child in kids:
            bundle_of[child.id] = name or "a bundle"

    def asks_for_something(line: OrderLine) -> bool:
        if line.id in has_children:
            return False
        if line.product_id is None:
            return line.state == LINE_UNMATCHED
        return _to_print(line) > 0 or _from_stock(line) > 0

    tallies = _column_tallies(everything, asks_for_something, chosen)
    lines = [line for line in everything if line.order.status in chosen]

    wanted = [
        line
        for line in lines
        if line.product_id is not None
        and line.id not in has_children
        and (_to_print(line) > 0 or _from_stock(line) > 0)
    ]
    makeable = await _makeable(session, wanted)

    rows: dict[tuple[Any, Any], dict[str, Any]] = {}
    for line in wanted:
        key = _key(line)
        product: Product | None = line.product
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "product_id": line.product_id,
                "variation_id": line.variation_id,
                "sku": product.sku if product else (line.sku_raw or "?"),
                "name": product.name if product else (line.title or "Unknown"),
                "variation_label": line.variation.label if line.variation else None,
                "fulfillment": product.fulfillment if product else None,
                # Whether anything here knows how to make it. False is a
                # person's job, not a machine's.
                "can_start": makeable.get(key, False),
                "units_needed": 0,
                # Under way, and how far along it is.
                "units_started": 0,
                "units_made": 0,
                "units_happening": 0,
                "started_at": None,
                # The half of the row that comes off a shelf. Still a thing to
                # go and get, so still on the list — just a different walk.
                "units_from_stock": 0,
                # Facts about the thing, gathered from the lines that want it.
                # The lines themselves are not kept: this is a list of things
                # to make, and a thing does not become several things to make
                # because several people bought it.
                "order_ids": set(),
                "waiting_since": None,
                "for_bundle": False,
                "bundles": set(),
            }

        row["units_needed"] += _to_print(line)
        row["units_from_stock"] += _from_stock(line)
        row["units_started"] += _units(line, UNDER_WAY)
        row["units_made"] += _units(line, FINISHED)
        row["units_happening"] += _units(line, HAPPENING)
        began = _started_at(line)
        if began is not None and (
            row["started_at"] is None or began < row["started_at"]
        ):
            row["started_at"] = began

        order = line.order
        row["order_ids"].add(order.id)
        placed = _when(order)
        if placed is not None and (
            row["waiting_since"] is None or placed < row["waiting_since"]
        ):
            row["waiting_since"] = placed
        # Worth saying when any of it is for a bundle: somebody picking the
        # shelf will not find the thing on an order by name.
        bundle = bundle_of.get(line.id)
        if bundle:
            row["for_bundle"] = True
            row["bundles"].add(bundle)

    to_do: list[dict[str, Any]] = []
    under_way: list[dict[str, Any]] = []
    for row in rows.values():
        row["order_count"] = len(row.pop("order_ids"))
        row["bundles"] = sorted(row.pop("bundles"))
        # Units nothing has started on, plus units nobody has fetched. The
        # second number is not a smaller kind of work: the shop is short of
        # them either way, so they are one quantity on one row.
        row["units_to_make"] = max(row["units_needed"] - row["units_started"], 0)
        row["units"] = row["units_to_make"] + row["units_from_stock"]
        # Nothing to print means nothing to be unable to print: a row that is
        # entirely off a shelf is never blocked, it is a walk.
        if row["units_to_make"] == 0:
            row["can_start"] = True
        if row["units"] > 0:
            to_do.append(row)
        if row["units_started"] > 0:
            under_way.append(_being_made(row))

    # Oldest first, because that is the order a shop works in; a tie goes to
    # the bigger job, which is the one worth setting up for.
    to_do.sort(
        key=lambda row: (
            row["waiting_since"] is None,
            row["waiting_since"],
            -row["units"],
        )
    )
    # What is closest to finished, first: it is the next thing off a machine.
    under_way.sort(key=lambda row: (-row["progress"], row["started_at"] is None))

    first, rest = await _split_out_priority(session, to_do)
    listed = first + rest

    return {
        "priority": first,
        "items": rest,
        "in_production": under_way,
        "blocked": [row for row in listed if not row["can_start"]],
        "unmatched": _unmatched(lines),
        "columns": tallies,
        "totals": _totals(listed, under_way, len({line.order_id for line in wanted})),
        "generated_at": datetime.now().astimezone(),
    }


def _column_tallies(
    everything: list[OrderLine],
    asks_for_something,
    chosen: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Every open board column, what it is holding, and whether it is showing.

    Counted over the whole shop and never over the filtered list, so the
    numbers are what you would get by pressing the chip rather than what you
    have got already. A count that fell to nothing the moment you excluded a
    column would leave somebody no way of finding their way back to it.

    Columns with nothing in them are still listed. A shop looking at an empty
    screen needs to see that Assembly is empty, not wonder where it went.
    """
    orders: dict[str, set[Any]] = {name: set() for name in OPEN_COLUMNS}
    # Grouped the same way the list itself is — per thing, within the
    # column — because the subtraction of work already under way happens
    # once per thing and not once per line. Counting the lines straight
    # would have a chip promise more than pressing it delivers, which is
    # the one thing a number on a filter must never do.
    needed: dict[tuple[str, Any, Any], list[int]] = {}
    for line in everything:
        column = line.order.status
        if column not in orders or not asks_for_something(line):
            continue
        orders[column].add(line.order_id)
        if line.product_id is None:
            # Nothing matched it, so nothing aggregates: it is its own row
            # and its own quantity.
            needed[(column, line.id, None)] = [line.quantity, 0, 0]
            continue
        tally = needed.setdefault((column, line.product_id, line.variation_id), [0, 0, 0])
        tally[0] += _to_print(line)
        tally[1] += _units(line, UNDER_WAY)
        tally[2] += _from_stock(line)

    units: dict[str, int] = {name: 0 for name in OPEN_COLUMNS}
    for (column, _, _), (to_print, started, from_stock) in needed.items():
        units[column] += max(to_print - started, 0) + from_stock

    return [
        {
            "key": name,
            "orders": len(orders[name]),
            "units": units[name],
            "showing": name in chosen,
        }
        for name in OPEN_COLUMNS
    ]


def _to_print(line: OrderLine) -> int:
    """Units of this line that have to come off a machine."""
    return line.qty_to_print if line.state not in MADE_STATES else 0


def _from_stock(line: OrderLine) -> int:
    """Units of this line that have to come off a shelf.

    A shelf does not empty itself when QuickBooks says the stock is there:
    somebody picks it when they pack it. So a stocked unit stays on the list
    right up to the label, which is the moment that happened.
    """
    return line.qty_from_stock if line.state not in GONE else 0


async def _split_out_priority(
    session: AsyncSession, to_do: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Lift the things the shop has pinned out of the list, in its order.

    A pinned thing with nothing outstanding still comes back, as a row
    reading zero. It is the only way "still first, nothing to do" is
    visible — and a priority a person cannot see is one they cannot take
    off, which is how a list like this silently rots.
    """
    pins = (
        (
            await session.execute(
                select(ProductionPriority).order_by(
                    ProductionPriority.position, ProductionPriority.created_at
                )
            )
        )
        .scalars()
        .all()
    )
    if not pins:
        return [], to_do

    by_key = {(row["product_id"], row["variation_id"]): row for row in to_do}
    first: list[dict[str, Any]] = []
    taken: set[tuple[Any, Any]] = set()
    for pin in pins:
        key = (pin.product_id, pin.variation_id)
        if key in taken:
            continue
        taken.add(key)
        row = by_key.get(key)
        first.append(row if row is not None else _quiet_pin(pin))

    return first, [row for row in to_do if (row["product_id"], row["variation_id"]) not in taken]


def _quiet_pin(pin: ProductionPriority) -> dict[str, Any]:
    """A pinned thing that nothing is waiting for, as a row of zeroes.

    Everything a real row has, so the screen needs no second shape for it —
    the only difference a reader sees is that the quantity is nought.
    """
    product = pin.product
    return {
        "product_id": pin.product_id,
        "variation_id": pin.variation_id,
        "sku": product.sku if product else "?",
        "name": product.name if product else "Unknown",
        "variation_label": pin.variation.label if pin.variation else None,
        "fulfillment": product.fulfillment if product else None,
        "can_start": True,
        "units_needed": 0,
        "units_started": 0,
        "units_made": 0,
        "units_happening": 0,
        "started_at": None,
        "units_from_stock": 0,
        "units_to_make": 0,
        "units": 0,
        "order_count": 0,
        "waiting_since": None,
        "for_bundle": False,
        "bundles": [],
    }


async def priority_keys(session: AsyncSession) -> list[tuple[Any, Any]]:
    """What is pinned, in order. The shop's answer to "what goes first"."""
    pins = (
        (
            await session.execute(
                select(ProductionPriority).order_by(
                    ProductionPriority.position, ProductionPriority.created_at
                )
            )
        )
        .scalars()
        .all()
    )
    return [(pin.product_id, pin.variation_id) for pin in pins]


async def set_priority(
    session: AsyncSession,
    wanted: list[tuple[Any, Any]],
    *,
    by: str | None = None,
) -> list[tuple[Any, Any]]:
    """Replace the whole priority list with this one, in this order.

    The whole list rather than one move, because that is what the screen
    actually knows: somebody dragged a row and the order they can see is now
    the order they mean. Sending "move X to 3" instead would have the server
    reconstruct a list it was never shown, and two people dragging at once
    would interleave into an order neither of them chose. Sending the list
    makes the last drag win, whole, which is the only outcome either of them
    would recognise.

    Raises ValueError for a thing that does not exist, or a variation that
    belongs to a different product — a pin on a thing nobody can make is a
    row that can never be acted on or explained.
    """
    seen: list[tuple[Any, Any]] = []
    for key in wanted:
        if key not in seen:
            seen.append(key)

    if seen:
        products = {
            row.id
            for row in (
                (
                    await session.execute(
                        select(Product).where(
                            Product.id.in_({key[0] for key in seen})
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        variations = {
            row.id: row.product_id
            for row in (
                (
                    await session.execute(
                        select(ProductVariation).where(
                            ProductVariation.id.in_(
                                {key[1] for key in seen if key[1] is not None}
                            )
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        for product_id, variation_id in seen:
            if product_id not in products:
                raise ValueError(f"No such product: {product_id}")
            if variation_id is not None and variations.get(variation_id) != product_id:
                raise ValueError(
                    f"Variation {variation_id} does not belong to product {product_id}"
                )

    existing = {
        (pin.product_id, pin.variation_id): pin
        for pin in (await session.execute(select(ProductionPriority))).scalars().all()
    }
    for position, key in enumerate(seen):
        pin = existing.pop(key, None)
        if pin is None:
            session.add(
                ProductionPriority(
                    product_id=key[0],
                    variation_id=key[1],
                    position=position,
                    pinned_by=by,
                )
            )
        else:
            pin.position = position
    # Whatever is left was dragged back off the list.
    for pin in existing.values():
        await session.delete(pin)
    await session.flush()
    return seen


def _being_made(row: dict[str, Any]) -> dict[str, Any]:
    """One thing under way, as a thing rather than as a pile of jobs.

    `waiting` is the remainder on purpose: units that are under way but
    neither finished nor moving are waiting their turn, and working it out by
    subtraction means the three numbers always add up to what is on the bar.
    """
    started = row["units_started"]
    made = min(row["units_made"], started)
    happening = min(row["units_happening"], started - made)
    return {
        "product_id": row["product_id"],
        "variation_id": row["variation_id"],
        "sku": row["sku"],
        "name": row["name"],
        "variation_label": row["variation_label"],
        "units": started,
        "units_made": made,
        "units_happening": happening,
        "units_waiting": started - made - happening,
        "progress": round(made / started, 3) if started else 0.0,
        "started_at": row["started_at"],
        "order_count": row["order_count"],
    }


def _totals(
    listed: list[dict[str, Any]], under_way: list[dict[str, Any]], orders: int
) -> dict[str, int]:
    # A pinned thing with nothing outstanding is on the screen but is not
    # work, so it is not in the count. A tile that said four when three
    # things needed doing would be the one number on the page nobody trusts.
    to_do = [row for row in listed if row["units"] > 0]
    return {
        "items": len(to_do),
        # The headline, and the two halves of it. The halves are on the tile
        # as a hint rather than as tiles of their own: one list, one number.
        "units": sum(row["units"] for row in to_do),
        "units_to_make": sum(row["units_to_make"] for row in to_do),
        "units_from_stock": sum(row["units_from_stock"] for row in to_do),
        "in_production_items": len(under_way),
        "units_in_production": sum(row["units"] for row in under_way),
        "units_happening": sum(row["units_happening"] for row in under_way),
        "blocked": sum(1 for row in to_do if not row["can_start"]),
        # How many orders are waiting on all of this. A count, not a
        # breakdown: the list is about things, and this is the one place the
        # orders are worth a number at all.
        "orders": orders,
    }


def _unmatched(lines: list[OrderLine]) -> list[dict[str, Any]]:
    """Things nobody can make because nothing says what they are.

    They belong on this list precisely because they are *not* on the rest of
    it: a line with no product contributes nothing to any total, and a screen
    that only counted what it understood would quietly under-report the work.

    Gathered by what the channel called them, the same way everything else
    here is gathered: the same unlinked listing sold six times is one thing to
    go and fix, not six.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for line in lines:
        if line.product_id is not None or line.state != LINE_UNMATCHED:
            continue
        key = (line.sku_raw or "", line.title or "")
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "sku": line.sku_raw,
                "title": line.title,
                "units": 0,
                "order_count": 0,
                "waiting_since": None,
                "order_ids": set(),
            }
        row["units"] += line.quantity
        placed = _when(line.order)
        if placed is not None and (
            row["waiting_since"] is None or placed < row["waiting_since"]
        ):
            row["waiting_since"] = placed
        row["order_ids"].add(line.order_id)

    out = []
    for row in rows.values():
        row["order_count"] = len(row.pop("order_ids"))
        out.append(row)
    out.sort(key=lambda entry: (entry["waiting_since"] is None, entry["waiting_since"]))
    return out
