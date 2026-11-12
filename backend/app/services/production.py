"""Everything the shop still has to make, and what is being made right now.

PrintFlow already knows this, scattered: the board knows which orders are
open, each order knows which of its lines are short, and the Printers tab
knows what is on a machine. What nobody could see was the shape a print shop
actually works to — *one thing, and how many of it*. Six orders each wanting
one bracket is six brackets, and finding that out meant opening six orders.

So this groups by **what is made rather than by who ordered it**: a row per
product and variation, because two variations of one product are made
differently and are two different things to make.

**Three buckets, and every unit is in exactly one of them.**

* *To make* — nothing has started on these. This is the list somebody works
  from, and the number they act on.
* *Being made* — work is under way. PrintFlow started it without being asked,
  so it is not a job for a person; it is something to watch finish.
* *Gathered* — taken off a shelf rather than off a machine, which is the
  other half of getting an order out and appears on no other screen.

How the making actually happens — which file, which machine, what was sent
where — is the Printers tab's business and deliberately not this one's. This
screen is a list of things and quantities.
"""

from __future__ import annotations

import logging
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
    LINE_ALLOCATED,
    LINE_CANCELLED,
    LINE_LABELED,
    LINE_PRINTED,
    LINE_READY,
    LINE_SHIPPED,
    LINE_UNMATCHED,
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_SHIPPED,
    Order,
    OrderLine,
    PrintFile,
    Product,
    ProductVariation,
)
from . import variations as variation_rules

log = logging.getLogger("printflow.production")

# Orders that have stopped asking for anything to be made. A shipped order's
# parts exist; a cancelled one's are not wanted.
CLOSED_ORDERS = (ORDER_SHIPPED, ORDER_COMPLETE, ORDER_CANCELLED)

# Lines whose making is behind them, whatever the order is doing.
MADE_STATES = (LINE_PRINTED, LINE_READY, LINE_LABELED, LINE_SHIPPED, LINE_CANCELLED)

# A component that physically exists and can be picked up. Not the same list:
# a part taken from stock is `allocated` and was never printed at all, and a
# bundle made entirely of shelf items would otherwise never look ready to
# build.
IN_HAND = (LINE_ALLOCATED, LINE_PRINTED, LINE_READY, LINE_LABELED, LINE_SHIPPED)

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


async def to_make(session: AsyncSession) -> dict[str, Any]:
    """What to make, what is being made, what to gather, and what is stuck.

    One query for the lines and their products — the shop floor refreshes
    this screen, and a query per row would make a list of forty things forty
    round trips.
    """
    lines = list(
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

    wanted = [
        line
        for line in lines
        if line.qty_to_print > 0
        and line.state not in MADE_STATES
        and line.product_id is not None
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
                # Facts about the thing, gathered from the lines that want it.
                # The lines themselves are not kept: this is a list of things
                # to make, and a thing does not become several things to make
                # because several people bought it.
                "order_ids": set(),
                "waiting_since": None,
                "for_bundle": False,
            }

        row["units_needed"] += line.qty_to_print
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
        row["for_bundle"] = row["for_bundle"] or line.parent_line_id is not None

    to_do: list[dict[str, Any]] = []
    under_way: list[dict[str, Any]] = []
    for row in rows.values():
        row["order_count"] = len(row.pop("order_ids"))
        # The number the list leads with: units nothing has started on.
        row["units_to_make"] = max(row["units_needed"] - row["units_started"], 0)
        if row["units_to_make"] > 0:
            to_do.append(row)
        if row["units_started"] > 0:
            under_way.append(_being_made(row))

    # Oldest first, because that is the order a shop works in; a tie goes to
    # the bigger job, which is the one worth setting up for.
    to_do.sort(
        key=lambda row: (
            row["waiting_since"] is None,
            row["waiting_since"],
            -row["units_to_make"],
        )
    )
    # What is closest to finished, first: it is the next thing off a machine.
    under_way.sort(key=lambda row: (-row["progress"], row["started_at"] is None))

    return {
        "items": to_do,
        "in_production": under_way,
        "blocked": [row for row in to_do if not row["can_start"]],
        "unmatched": _unmatched(lines),
        "gather": _gather(lines),
        "totals": _totals(to_do, under_way, len({line.order_id for line in wanted})),
        "generated_at": datetime.now().astimezone(),
    }


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
    to_do: list[dict[str, Any]], under_way: list[dict[str, Any]], orders: int
) -> dict[str, int]:
    return {
        "items": len(to_do),
        "units_to_make": sum(row["units_to_make"] for row in to_do),
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


def _gather(lines: list[OrderLine]) -> list[dict[str, Any]]:
    """Everything an open order needs off a shelf rather than off a printer.

    Two kinds of thing, and one rule covers both: **if it is a bundle, use its
    components; otherwise use the item itself.**

    * A bundle waiting to be built is not a job somebody can pick up — six
      brackets and three knobs is. So a bundle is never a row here; its parts
      are, added up across every bundle and order waiting, with the bundle's
      name riding along as what they are for.
    * An item that is not a bundle has nothing to break into, so it is the row.
      That is how the things an order takes from stock get onto this screen at
      all: nothing is printed for them, so they appear on no other list here,
      and somebody still has to walk and find them.

    Components are taken from the order's own lines rather than from the bill
    of materials, because the two can differ: an option the buyer picked can
    swap a component out, and the line is what that decision was recorded on.
    """
    by_parent: dict[Any, list[OrderLine]] = {}
    for line in lines:
        if line.parent_line_id is not None:
            by_parent.setdefault(line.parent_line_id, []).append(line)

    rows: dict[tuple[Any, Any], dict[str, Any]] = {}

    def row_for(line: OrderLine) -> dict[str, Any]:
        product: Product | None = line.product
        key = (line.product_id or (line.sku_raw or line.title), line.variation_id)
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                "product_id": line.product_id,
                "variation_id": line.variation_id,
                "sku": product.sku if product else (line.sku_raw or "?"),
                "name": product.name if product else (line.title or "Unknown"),
                "variation_label": line.variation.label if line.variation else None,
                "fulfillment": product.fulfillment if product else None,
                # Units that can be fetched now, and units belonging to
                # bundles that are still missing a part of their own.
                "units": 0,
                "units_waiting": 0,
                "bundles": set(),
                "order_ids": set(),
                "waiting_since": None,
            }
        return row

    def note(row: dict[str, Any], line: OrderLine) -> None:
        row["order_ids"].add(line.order_id)
        placed = _when(line.order)
        if placed is not None and (
            row["waiting_since"] is None or placed < row["waiting_since"]
        ):
            row["waiting_since"] = placed

    for line in lines:
        # Already out of the door, or never going out of it.
        if line.state in (LINE_CANCELLED, LINE_SHIPPED, LINE_LABELED):
            continue

        children = by_parent.get(line.id)
        if children:
            # A bundle: its parts, never itself. Nothing to gather once it has
            # been put together.
            if line.assembled_at is not None:
                continue
            # Whether this particular bundle can be built now. A half-made one
            # is a bench occupied by something that cannot be finished, so its
            # components are counted apart rather than added in. A cancelled
            # component is not something to wait for.
            buildable = all(
                child.state in IN_HAND
                for child in children
                if child.state != LINE_CANCELLED
            )
            bundle = (
                line.product.name if line.product else (line.title or line.sku_raw)
            ) or "bundle"
            for child in children:
                if child.state == LINE_CANCELLED:
                    continue
                row = row_for(child)
                row["units" if buildable else "units_waiting"] += child.quantity
                row["bundles"].add(bundle)
                note(row, line)
            continue

        # Not a bundle, and not a bundle's component — those were counted with
        # their bundle. What is left is an item in its own right, and the part
        # of it that comes off a shelf is what there is to go and fetch. The
        # part being printed is on the print list instead; a line that is half
        # stock and half print is honestly on both.
        if line.parent_line_id is not None or line.qty_from_stock <= 0:
            continue
        row = row_for(line)
        row["units"] += line.qty_from_stock
        note(row, line)

    out = []
    for row in rows.values():
        row["bundles"] = sorted(row.pop("bundles"))
        row["order_count"] = len(row.pop("order_ids"))
        out.append(row)
    # What can be fetched now, first.
    out.sort(
        key=lambda entry: (
            not entry["units"],
            entry["waiting_since"] is None,
            entry["waiting_since"],
        )
    )
    return out
