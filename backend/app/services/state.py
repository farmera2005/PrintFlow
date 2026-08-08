"""Line state machine and order roll-up (§5).

The transition rules are pure functions over plain values so they can be
unit-tested without a database. `recompute_order` is the only DB-aware entry
point; everything that mutates lines or print jobs calls it afterwards.

Order status is *derived* and never set directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import (
    JOB_CANCELLED,
    JOB_DONE,
    JOB_FAILED,
    JOB_OPEN_STATUSES,
    JOB_PENDING,
    LINE_ALLOCATED,
    LINE_CANCELLED,
    LINE_EXPLODED,
    LINE_LABELED,
    LINE_NEW,
    LINE_PRINTED,
    LINE_PRINTING,
    LINE_READY,
    LINE_SHIPPED,
    LINE_UNMATCHED,
    ORDER_ASSEMBLY,
    ORDER_CANCELLED,
    ORDER_IN_PRODUCTION,
    ORDER_NEW,
    ORDER_READY_TO_SHIP,
    ORDER_SHIPPED,
    Order,
    OrderLine,
    PrintJob,
)

# States meaning "this line's own production work is finished".
WORK_COMPLETE_STATES = (LINE_ALLOCATED, LINE_PRINTED)
# States at or past "ready" — nothing left to produce or assemble.
TERMINAL_READY_STATES = (LINE_READY, LINE_LABELED, LINE_SHIPPED)

# Overrides an operator may apply to a line from the card menu.
ALLOWED_LINE_OVERRIDES = (LINE_PRINTED, LINE_READY, LINE_CANCELLED)


@dataclass
class LineView:
    """Everything the roll-up needs to know about one line."""

    state: str
    is_bundle: bool = False
    is_leaf: bool = True
    has_jobs: bool = False
    has_failed_job: bool = False
    assembled: bool = False


# --------------------------------------------------------------------------
# Pure transition rules
# --------------------------------------------------------------------------


def compute_leaf_line_state(
    *,
    matched: bool,
    qty_to_print: int,
    job_statuses: tuple[str, ...] | list[str],
    decided: bool,
    override_state: str | None = None,
    parent_is_bundle: bool = False,
    parent_assembled: bool = False,
    order_labeled: bool = False,
    order_shipped: bool = False,
) -> str:
    """State of a line that is actually produced (never a bundle container)."""
    if override_state == LINE_CANCELLED:
        return LINE_CANCELLED
    if order_shipped:
        return LINE_SHIPPED
    if order_labeled:
        return LINE_LABELED
    if not matched:
        return LINE_UNMATCHED

    if override_state in (LINE_PRINTED, LINE_READY):
        work_state = LINE_PRINTED
    elif not decided:
        # Intake matched the SKU but the stock/print decision has not run yet.
        return LINE_NEW
    elif qty_to_print <= 0:
        work_state = LINE_ALLOCATED
    else:
        statuses = tuple(job_statuses)
        if not statuses:
            # Decided to print, no plates worked out yet.
            return LINE_NEW
        if all(s == JOB_PENDING for s in statuses):
            # Plates planned but not yet pushed to Bambuddy — still "New" on the
            # board, which is what §5 means by "decisions made, nothing printing".
            return LINE_NEW
        if any(s in JOB_OPEN_STATUSES for s in statuses):
            return LINE_PRINTING
        if all(s == JOB_DONE for s in statuses):
            work_state = LINE_PRINTED
        else:
            # Every job settled but at least one failed or was cancelled. Hold the
            # line in `printing` so the board keeps it in production and flags red
            # until the operator re-queues or overrides.
            return LINE_PRINTING

    # A component of a bundle is not `ready` until the bundle is assembled.
    if parent_is_bundle and not parent_assembled:
        return work_state
    return LINE_READY


def compute_bundle_line_state(
    *,
    child_states: tuple[str, ...] | list[str],
    override_state: str | None = None,
    order_labeled: bool = False,
    order_shipped: bool = False,
) -> str:
    """State of a bundle container line — a roll-up of its component lines."""
    if override_state == LINE_CANCELLED:
        return LINE_CANCELLED
    if order_shipped:
        return LINE_SHIPPED
    if order_labeled:
        return LINE_LABELED

    states = [s for s in child_states]
    if not states:
        return LINE_NEW
    active = [s for s in states if s != LINE_CANCELLED]
    if not active:
        return LINE_CANCELLED
    if all(s in TERMINAL_READY_STATES for s in active):
        return LINE_READY
    return LINE_EXPLODED


def compute_order_status(lines: list[LineView], *, label_created: bool = False) -> str:
    """Derive the board column for an order from its lines."""
    if not lines:
        return ORDER_NEW
    active = [line for line in lines if line.state != LINE_CANCELLED]
    if not active:
        return ORDER_CANCELLED
    if label_created or all(line.state == LINE_SHIPPED for line in active):
        return ORDER_SHIPPED

    leaves = [line for line in active if line.is_leaf]
    if not leaves:
        return ORDER_NEW

    # An unmatched SKU needs the operator before the order can progress, so the
    # card renders in New with a red badge even if its other lines are already
    # printing (§5) — otherwise the fix path hides in a later column.
    if any(line.state == LINE_UNMATCHED for line in leaves):
        return ORDER_NEW

    if all(line.state in TERMINAL_READY_STATES for line in leaves):
        return ORDER_READY_TO_SHIP

    production_done = all(
        line.state in WORK_COMPLETE_STATES + TERMINAL_READY_STATES for line in leaves
    )
    unassembled_bundles = [
        line for line in active if line.is_bundle and not line.assembled
    ]
    if production_done and unassembled_bundles:
        return ORDER_ASSEMBLY

    if any(line.state == LINE_PRINTING for line in leaves) or any(
        line.has_jobs for line in leaves
    ):
        return ORDER_IN_PRODUCTION

    if any(line.state in WORK_COMPLETE_STATES for line in leaves):
        return ORDER_IN_PRODUCTION

    return ORDER_NEW


def order_needs_attention(lines: list[LineView]) -> bool:
    return any(
        line.state == LINE_UNMATCHED or line.has_failed_job
        for line in lines
        if line.state != LINE_CANCELLED
    )


# --------------------------------------------------------------------------
# Database-aware recompute
# --------------------------------------------------------------------------


@dataclass
class _Node:
    line: OrderLine
    children: list[OrderLine] = field(default_factory=list)
    job_statuses: list[str] = field(default_factory=list)


async def load_order_graph(session: AsyncSession, order_id) -> Order | None:
    result = await session.execute(
        select(Order)
        .where(Order.id == order_id)
        .options(selectinload(Order.lines).selectinload(OrderLine.print_jobs))
    )
    return result.scalar_one_or_none()


async def recompute_order(session: AsyncSession, order: Order) -> str:
    """Recompute every line state and the order roll-up. Idempotent."""
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
    by_id = {line.id: line for line in lines}
    children: dict[object, list[OrderLine]] = {}
    for line in lines:
        if line.parent_line_id is not None:
            children.setdefault(line.parent_line_id, []).append(line)

    order_shipped = order.tracking_number is not None and order.label_created_at is not None
    order_labeled = order.label_created_at is not None and not order_shipped

    # Leaves first: bundle roll-ups read their children's freshly computed states.
    leaf_lines = [line for line in lines if not children.get(line.id)]
    for line in leaf_lines:
        parent = by_id.get(line.parent_line_id) if line.parent_line_id else None
        parent_is_bundle = parent is not None
        line.state = compute_leaf_line_state(
            matched=line.product_id is not None,
            qty_to_print=line.qty_to_print,
            job_statuses=[job.status for job in line.print_jobs],
            decided=line.decided_at is not None,
            override_state=line.override_state,
            parent_is_bundle=parent_is_bundle,
            parent_assembled=bool(parent and parent.assembled_at),
            order_labeled=order_labeled,
            order_shipped=order_shipped,
        )

    for line in lines:
        kids = children.get(line.id)
        if not kids:
            continue
        line.state = compute_bundle_line_state(
            child_states=[kid.state for kid in kids],
            override_state=line.override_state,
            order_labeled=order_labeled,
            order_shipped=order_shipped,
        )

    views = [
        LineView(
            state=line.state,
            is_bundle=bool(children.get(line.id)),
            is_leaf=not children.get(line.id),
            has_jobs=any(job.status != JOB_PENDING for job in line.print_jobs),
            has_failed_job=any(
                job.status in (JOB_FAILED, JOB_CANCELLED) for job in line.print_jobs
            ),
            assembled=line.assembled_at is not None,
        )
        for line in lines
    ]
    order.status = compute_order_status(views, label_created=order.label_created_at is not None)
    await session.flush()
    return order.status


async def recompute_order_by_id(session: AsyncSession, order_id) -> str | None:
    order = await session.get(Order, order_id)
    if order is None:
        return None
    return await recompute_order(session, order)


async def recompute_order_for_line(session: AsyncSession, line: OrderLine) -> str | None:
    return await recompute_order_by_id(session, line.order_id)


async def recompute_order_for_job(session: AsyncSession, job: PrintJob) -> str | None:
    line = await session.get(OrderLine, job.order_line_id)
    if line is None:
        return None
    return await recompute_order_for_line(session, line)
