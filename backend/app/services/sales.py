"""What the shop sold, by week, month, quarter or year.

Every figure here is already on the orders — this adds no new source of truth,
it groups the one that exists. Which makes the interesting decisions the three
that grouping forces:

**Which date.** A sale is dated when the buyer placed it, not when the parcel
left. A report dated by despatch would move a December sale into January
because it was posted late, and the question a shop asks of a sales report is
"what did we sell in December".

**Whose calendar.** A week is only a week somewhere. Bucketing in UTC puts a
Sunday-evening sale in Ohio into the following week, and the first hours of a
month into the one before — so the boundaries are drawn in the timezone the
report is read in, and the report says which one that was.

**What counts as done.** An order still being printed has sold — the buyer has
paid and the money is committed — but it is not the same money as an order that
went out of the door, and a shop deciding what it can spend needs to see the
two apart. So every figure is split: *processed* for orders that have shipped
or completed, *in progress* for everything still on the board. Cancelled orders
are in neither and counted on their own.

A fourth number rides alongside, because it answers what the other three
cannot: how much of it has been **invoiced into QuickBooks**. Fulfilment and
billing are different kinds of done, and reconciling a month against the books
means seeing both.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Numeric, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_SHIPPED,
    ORDER_SOURCES,
    Order,
)
from .manufacturing import money

log = logging.getLogger("printflow.sales")


class SalesError(RuntimeError):
    """The report cannot be drawn as asked. Shown verbatim."""


# The four grains. A shop asks the same question at four zoom levels — how was
# this week, how is the month going, what did the quarter do, how do the years
# compare — and they are one query with a different truncation.
GRAINS = ("week", "month", "quarter", "year")
GRAIN_LABELS = {
    "week": "Weekly",
    "month": "Monthly",
    "quarter": "Quarterly",
    "year": "Annual",
}

# How many periods a report covers by default, per grain. Each lands on a span
# somebody actually compares across: a quarter of weeks, a year of months, two
# years of quarters, five years.
DEFAULT_SPAN = {"week": 13, "month": 12, "quarter": 8, "year": 5}
MAX_SPAN = 200

# The money on an order, in the order a receipt reads it. `revenue` is the
# whole of it and the rest is how it was made up, so they are shown as a
# breakdown rather than summed — adding items to revenue would count the sale
# twice.
TAKINGS = ("revenue", "items_total", "shipping_total", "tax_total", "discount_total")
COSTS = ("etsy_fees", "marketing_fees", "processing_fees", "label_cost")
FIGURES = (*TAKINGS, *COSTS)

# Out of the door. Everything else that is not cancelled is still in the shop.
DONE_STATUSES = (ORDER_SHIPPED, ORDER_COMPLETE)

# The three columns of every row, and the status each one asks about.
SPLITS = ("all", "done", "live")


def zone(name: str | None) -> ZoneInfo:
    """The timezone to draw period boundaries in, falling back to UTC.

    An unknown zone is not worth failing a report over: a browser sending
    something this machine has never heard of should get its figures in UTC and
    a line saying so, rather than an error where the sales were.
    """
    if not name:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(str(name))
    except (ZoneInfoNotFoundError, ValueError):
        log.info("Unknown timezone %r for a sales report; using UTC", name)
        return ZoneInfo("UTC")


def period_start(when: date, grain: str) -> date:
    """The first day of the period `when` falls in.

    The same arithmetic Postgres does, done here as well, so the list of
    periods is worked out before the query rather than read off its results —
    a month with no sales has to appear as a zero, and a month that produced no
    row cannot report itself.
    """
    if grain == "week":
        # Monday, which is where Postgres' date_trunc('week') cuts too.
        return when - timedelta(days=when.weekday())
    if grain == "month":
        return when.replace(day=1)
    if grain == "quarter":
        return when.replace(month=((when.month - 1) // 3) * 3 + 1, day=1)
    if grain == "year":
        return when.replace(month=1, day=1)
    raise SalesError(f"Unknown grain {grain!r}.")


def shift(start: date, grain: str, periods: int) -> date:
    """`periods` whole periods after `start` (negative goes back)."""
    if grain == "week":
        return start + timedelta(days=7 * periods)
    months = {"month": 1, "quarter": 3, "year": 12}[grain] * periods
    total = (start.year * 12 + start.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def period_label(start: date, grain: str) -> str:
    """What to call this bucket on a screen — the way somebody would say it.

    "Sep 2026" and "Q3 2026" are read at a glance; "2026-09-01" has to be
    decoded first.
    """
    if grain == "week":
        return f"Week of {start.strftime('%d %b %Y').lstrip('0')}"
    if grain == "month":
        return start.strftime("%b %Y")
    if grain == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    return str(start.year)


def _when():
    """When the sale happened.

    Falling back to when PrintFlow first saw it, for the same reason the fee
    sweep does: an order whose channel sent no timestamp is still a sale, and
    one that could never be bucketed would be missing from every report.
    """
    return func.coalesce(Order.placed_at, Order.created_at)


def _bucket(grain: str, tz_name: str):
    """Which period an order belongs to, cut in the reader's timezone.

    `timezone(zone, timestamptz)` is Postgres' AT TIME ZONE: it reads the
    instant as a local clock time, so the truncation falls where the shop's
    week does. The zone travels as a bind parameter, never as text spliced
    into a statement.
    """
    return func.date_trunc(grain, func.timezone(tz_name, _when()))


def _sum(column, *, when=None):
    """A money sum that is 0 rather than NULL when nothing matched.

    A month with no sales has to read as 0.00 and not as a blank that looks
    like a bug — and a sum over no rows is NULL in every SQL there is.
    """
    target = column if when is None else case((when, column), else_=None)
    return func.coalesce(func.sum(target), 0).cast(Numeric(14, 4))


def _count(when):
    return func.count(case((when, Order.id), else_=None))


def _decimal(value: Any) -> Decimal:
    return money(value if value is not None else 0)


def _strings(figures: dict[str, Decimal]) -> dict[str, str]:
    """Money on its way to a screen: decimal strings, because JSON's only
    number cannot hold 7.41 exactly and this is a figure somebody reconciles
    against a statement."""
    return {name: str(amount) for name, amount in figures.items()}


def _zeros() -> dict[str, Decimal]:
    return {name: Decimal("0.00") for name in (*FIGURES, "net")}


def net_of(figures: dict[str, Decimal]) -> Decimal:
    """Revenue less every fee and the postage — `finance.net_of`, over a pile
    of orders rather than one of them."""
    return figures["revenue"] - sum((figures[name] for name in COSTS), Decimal("0.00"))


def _statuses():
    """The three questions each row answers, as SQL."""
    return {
        "all": Order.status != ORDER_CANCELLED,
        "done": Order.status.in_(DONE_STATUSES),
        "live": Order.status.notin_((*DONE_STATUSES, ORDER_CANCELLED)),
    }


def _since(earliest: date, where: ZoneInfo) -> datetime:
    """The instant the first period begins, as a moment in time.

    Local midnight rather than UTC midnight, so the range starts where the
    period does — the same boundary the bucketing uses, which is what keeps an
    order on the edge from landing outside a report that should contain it.
    """
    return datetime.combine(earliest, time.min, tzinfo=where)


async def report(
    session: AsyncSession,
    *,
    grain: str = "month",
    periods: int | None = None,
    tz_name: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Sales by period, split into what has shipped and what has not.

    Grouped in the database, in two queries: a shop with three years of orders
    should not send three years of orders to a browser to be added up there.
    """
    if grain not in GRAINS:
        raise SalesError(
            f"A sales report can be weekly, monthly, quarterly or annual, not {grain!r}."
        )
    wanted = periods if periods is not None else DEFAULT_SPAN[grain]
    if wanted < 1:
        raise SalesError("A report covers at least one period.")
    span = min(wanted, MAX_SPAN)

    where = zone(tz_name)
    tz_name = str(where.key)
    now = datetime.now(timezone.utc).astimezone(where)
    latest = period_start(today or now.date(), grain)
    earliest = shift(latest, grain, -(span - 1))
    since = _since(earliest, where)

    bucket = _bucket(grain, tz_name)
    states = _statuses()
    cancelled = Order.status == ORDER_CANCELLED
    invoiced = and_(states["all"], Order.qbo_invoice_id.isnot(None))

    columns: list[Any] = [bucket.label("bucket")]
    for split in SPLITS:
        prefix = "" if split == "all" else f"{split}_"
        columns.append(_count(states[split]).label(f"{prefix}orders"))
        columns += [
            _sum(getattr(Order, field), when=states[split]).label(f"{prefix}{field}")
            for field in FIGURES
        ]
    columns += [
        _count(cancelled).label("cancelled_orders"),
        _count(invoiced).label("invoiced_orders"),
        _sum(Order.qbo_invoice_total, when=invoiced).label("invoiced_total"),
    ]

    rows = (
        await session.execute(
            select(*columns).where(_when() >= since).group_by(bucket).order_by(bucket)
        )
    ).all()
    found = {_row_date(row.bucket): row for row in rows}

    out: list[dict[str, Any]] = []
    counts = {f"{key}_orders": 0 for key in ("all", "done", "live", "cancelled", "invoiced")}
    money_totals = {split: _zeros() for split in SPLITS}
    invoiced_total = Decimal("0.00")

    start = earliest
    while start <= latest:
        row = found.get(start)
        entry: dict[str, Any] = {
            "start": start.isoformat(),
            "end": (shift(start, grain, 1) - timedelta(days=1)).isoformat(),
            "label": period_label(start, grain),
            # The period being read from inside it. A month three days old is
            # not a month that sold badly, and a screen that does not say so
            # invites exactly that reading.
            "partial": start == latest,
        }
        for split in SPLITS:
            prefix = "" if split == "all" else f"{split}_"
            figures = {
                name: _decimal(getattr(row, f"{prefix}{name}") if row else None)
                for name in FIGURES
            }
            figures["net"] = net_of(figures)
            entry[split] = _strings(figures)
            entry[f"{split}_orders"] = int(getattr(row, f"{prefix}orders") if row else 0)
            counts[f"{split}_orders"] += entry[f"{split}_orders"]
            for name, amount in figures.items():
                money_totals[split][name] += amount
        entry["cancelled_orders"] = int(row.cancelled_orders if row else 0)
        entry["invoiced_orders"] = int(row.invoiced_orders if row else 0)
        entry["invoiced_total"] = str(_decimal(row.invoiced_total if row else None))
        counts["cancelled_orders"] += entry["cancelled_orders"]
        counts["invoiced_orders"] += entry["invoiced_orders"]
        invoiced_total += _decimal(entry["invoiced_total"])
        out.append(entry)
        start = shift(start, grain, 1)

    return {
        "grain": grain,
        "grain_label": GRAIN_LABELS[grain],
        "timezone": tz_name,
        "from": earliest.isoformat(),
        "to": (shift(latest, grain, 1) - timedelta(days=1)).isoformat(),
        "periods": out,
        "totals": {
            **counts,
            "invoiced_total": str(invoiced_total),
            **{split: _strings(money_totals[split]) for split in SPLITS},
        },
        "channels": await channel_totals(session, grain=grain, tz_name=tz_name, since=since),
        "currency": await _currency(session),
    }


def _row_date(value: Any) -> date:
    """The bucket a row came back as, as a plain date.

    `date_trunc` over a local clock time returns a timestamp without a zone, so
    this is a naive datetime; taking its date is what lines it up with the
    period list built in Python.
    """
    return value.date() if isinstance(value, datetime) else value


async def _currency(session: AsyncSession) -> str | None:
    """What the figures are in. One shop, one currency in practice — so this is
    the most recent order's, and None when nothing says."""
    return (
        await session.execute(
            select(Order.currency)
            .where(Order.currency.isnot(None))
            .order_by(_when().desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def channel_totals(
    session: AsyncSession, *, grain: str, tz_name: str, since: datetime
) -> list[dict[str, Any]]:
    """The same range, split by which shop window sold it.

    A shop running three channels wants to know which is carrying the month; a
    shop running one sees a single row and has lost nothing.
    """
    states = _statuses()
    rows = (
        await session.execute(
            select(
                Order.source,
                _count(states["all"]).label("orders"),
                _sum(Order.revenue, when=states["all"]).label("revenue"),
                _sum(Order.revenue, when=states["done"]).label("done_revenue"),
                _sum(Order.revenue, when=states["live"]).label("live_revenue"),
            )
            .where(_when() >= since)
            .group_by(Order.source)
        )
    ).all()

    found = {row.source: row for row in rows}
    out = []
    for source in ORDER_SOURCES:
        row = found.get(source)
        # A channel that sold nothing in the range is left out rather than
        # shown as a row of zeroes: a shop that does not use Wix should not
        # have to read a line about Wix on every report.
        if row is None or not int(row.orders):
            continue
        out.append(
            {
                "source": source,
                "orders": int(row.orders),
                "revenue": str(_decimal(row.revenue)),
                "done_revenue": str(_decimal(row.done_revenue)),
                "live_revenue": str(_decimal(row.live_revenue)),
            }
        )
    return out


async def open_orders(session: AsyncSession, *, limit: int = 100) -> list[dict[str, Any]]:
    """Every order still in the shop, oldest first.

    The other half of the report: the periods say how much is still in
    progress, this says which orders that is — oldest first, because the one
    that has been in progress longest is the one worth looking at.
    """
    rows = (
        (
            await session.execute(
                select(Order)
                .where(Order.status.notin_((*DONE_STATUSES, ORDER_CANCELLED)))
                .order_by(_when().asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": order.id,
            "order_number": order.order_number,
            "source": order.source,
            "buyer_name": order.buyer_name,
            "placed_at": order.placed_at,
            "status": order.status,
            "revenue": str(money(order.revenue)) if order.revenue is not None else None,
            "currency": order.currency,
            "invoiced": order.qbo_invoice_id is not None,
        }
        for order in rows
    ]
