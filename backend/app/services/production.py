"""Everything the shop still has to make, gathered into one list.

PrintFlow already knows this, scattered: the board knows which orders are
open, each order knows which of its lines are short, and the Printers tab
knows what is on a machine. What nobody could see was the shape a print shop
actually works to — *one part, however many orders want it*. Six orders each
needing one bracket is one plate's work, and finding that out meant opening
six orders.

So this groups by **what is made rather than by who ordered it**: a row per
product and variation, because a variation can print a different file
entirely and two variations of one product are two different things to make.
Underneath each row sit the orders it came from, oldest first, which is the
order a shop works in.

**What counts as needing production.** A line that is still short — some
units have to be printed — and whose work is not finished. Units coming off
the shelf are picked, not produced, so they are not here; a line already
printed is not here either, however far from shipping its order is.

**Plates are coverage, not progress.** A line's shortfall is covered by
plates, and a plate can be planned, sent, printing, done or failed. The
difference between the units a row needs and the units its live plates
promise is the only number that says what is genuinely not in hand yet, and
it is the number this list leads with.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
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

# Plates that still stand for units nobody has to print again. A failed or
# cancelled plate covers nothing — which is exactly why those units have to
# come back onto this list rather than look finished.
LIVE_JOBS = (JOB_PENDING, JOB_QUEUED, JOB_PRINTING, JOB_DONE)

# What a row is waiting on, in the order a person would act on it.
BLOCKED = "blocked"
TO_PLAN = "to_plan"
TO_SEND = "to_send"
ON_A_MACHINE = "on_a_machine"
STAGE_LABELS = {
    BLOCKED: "Cannot be made yet",
    TO_PLAN: "No plates worked out",
    TO_SEND: "Ready to send",
    ON_A_MACHINE: "On a machine",
}


def _key(line: OrderLine) -> tuple[Any, Any]:
    """What makes two lines the same thing to make.

    The variation is part of it: "with fan" and "without fan" are different
    parts off different plates, and adding them together would produce a
    number no single plate can satisfy.
    """
    return (line.product_id, line.variation_id)


def _job_counts(line: OrderLine) -> dict[str, int]:
    counts = {status: 0 for status in (JOB_PENDING, JOB_QUEUED, JOB_PRINTING, JOB_DONE, JOB_FAILED, JOB_CANCELLED)}
    for job in line.print_jobs:
        if job.status in counts:
            counts[job.status] += 1
    return counts


def _covered(line: OrderLine) -> int:
    """Units this line's live plates promise.

    `units_expected` rather than the plate count: a plate yields what its file
    yields, and four-up plates are the ordinary case for small parts.
    """
    return sum(job.units_expected for job in line.print_jobs if job.status in LIVE_JOBS)


async def _plans_for(
    session: AsyncSession, lines: list[OrderLine]
) -> dict[tuple[Any, Any], list[Any]]:
    """How each product-and-variation could be printed, if it can be at all.

    The same question `printing.plan_jobs` asks, asked once for the whole list
    rather than per line, because the answer is what separates "nothing has
    been planned yet" from "nothing can be planned, and here is why".
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

    plans: dict[tuple[Any, Any], list[Any]] = {}
    for line in lines:
        key = _key(line)
        if key in plans or not line.product_id:
            continue
        plans[key] = variation_rules.print_plans(
            files.get(line.product_id, []), found_variations.get(line.variation_id)
        )
    return plans


def _when(order: Order) -> datetime | None:
    return order.placed_at or order.created_at


async def to_make(session: AsyncSession) -> dict[str, Any]:
    """The whole list: what to make, what is blocked, and what to assemble.

    One query for the lines and one for their orders' products — the shop
    floor refreshes this screen, and a query per row would make a list of
    forty parts forty round trips.
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
    plans = await _plans_for(session, wanted)

    rows: dict[tuple[Any, Any], dict[str, Any]] = {}
    for line in wanted:
        key = _key(line)
        product: Product | None = line.product
        row = rows.get(key)
        if row is None:
            plan = plans.get(key) or []
            row = rows[key] = {
                "product_id": line.product_id,
                "variation_id": line.variation_id,
                "sku": product.sku if product else (line.sku_raw or "?"),
                "name": product.name if product else (line.title or "Unknown"),
                "variation_label": line.variation.label if line.variation else None,
                "fulfillment": product.fulfillment if product else None,
                # What one plate yields, taken the way the planner takes it:
                # the least any candidate file promises, because the plate has
                # to cover the line whichever machine ends up printing it.
                "units_per_plate": (
                    min(candidate.units_per_plate for candidate in plan) if plan else None
                ),
                "printable": bool(plan),
                "units_needed": 0,
                "units_covered": 0,
                # Facts about the part, gathered from the lines that want it.
                # The lines themselves are not kept: this is a list of things
                # to make, and a part does not become several things to make
                # because several people bought it.
                "order_ids": set(),
                "waiting_since": None,
                "for_bundle": False,
                "note": None,
                "jobs": {
                    status: 0
                    for status in (
                        JOB_PENDING, JOB_QUEUED, JOB_PRINTING, JOB_DONE, JOB_FAILED, JOB_CANCELLED
                    )
                },
            }

        row["units_needed"] += line.qty_to_print
        row["units_covered"] += _covered(line)
        for status, count in _job_counts(line).items():
            row["jobs"][status] += count

        order = line.order
        row["order_ids"].add(order.id)
        placed = _when(order)
        if placed is not None and (
            row["waiting_since"] is None or placed < row["waiting_since"]
        ):
            row["waiting_since"] = placed
        # Worth saying when any of it is for a bundle: somebody picking the
        # shelf will not find the part on an order by name.
        row["for_bundle"] = row["for_bundle"] or line.parent_line_id is not None
        row["note"] = row["note"] or line.stock_note

    made: list[dict[str, Any]] = []
    for row in rows.values():
        row["order_count"] = len(row.pop("order_ids"))
        # The number the list leads with: units nobody has a live plate for.
        row["units_outstanding"] = max(row["units_needed"] - row["units_covered"], 0)
        row["plates_outstanding"] = (
            -(-row["units_outstanding"] // row["units_per_plate"])
            if row["units_per_plate"] and row["units_outstanding"] > 0
            else 0
        )
        row["stage"] = _stage(row)
        row["stage_label"] = STAGE_LABELS[row["stage"]]
        made.append(row)

    # Oldest first, because that is the order a shop works in; a tie goes to
    # the bigger job, which is the one worth setting a machine up for.
    made.sort(
        key=lambda row: (
            row["waiting_since"] is None,
            row["waiting_since"],
            -row["units_outstanding"],
        )
    )

    return {
        "items": made,
        "blocked": [row for row in made if row["stage"] == BLOCKED],
        "unmatched": _unmatched(lines),
        "gather": _gather(lines),
        "totals": _totals(made, len({line.order_id for line in wanted})),
        "generated_at": datetime.now().astimezone(),
    }


def _stage(row: dict[str, Any]) -> str:
    """What this row is waiting on."""
    if not row["printable"]:
        return BLOCKED
    if row["jobs"][JOB_QUEUED] or row["jobs"][JOB_PRINTING]:
        return ON_A_MACHINE
    if row["jobs"][JOB_PENDING]:
        return TO_SEND
    return TO_PLAN


def _totals(rows: list[dict[str, Any]], orders: int) -> dict[str, int]:
    return {
        "items": len(rows),
        "units_needed": sum(row["units_needed"] for row in rows),
        "units_outstanding": sum(row["units_outstanding"] for row in rows),
        "plates_to_send": sum(row["jobs"][JOB_PENDING] for row in rows),
        "plates_on_machines": sum(
            row["jobs"][JOB_QUEUED] + row["jobs"][JOB_PRINTING] for row in rows
        ),
        "blocked": sum(1 for row in rows if row["stage"] == BLOCKED),
        # How many orders are waiting on all of this. A count, not a
        # breakdown: the list is about parts, and this is the one place the
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
