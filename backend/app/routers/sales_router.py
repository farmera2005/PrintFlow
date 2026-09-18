"""The sales report: one read, and the same figures as a spreadsheet.

Nothing here writes anything. A report is a question about orders that already
exist, so every route is a GET and none of them touches an integration.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..models import User
from ..services import sales
from ..services.sales import SalesError

router = APIRouter(prefix="/api/sales", tags=["sales"])


async def _report(
    session: AsyncSession, grain: str, periods: int | None, tz: str | None
) -> dict[str, Any]:
    try:
        return await sales.report(session, grain=grain, periods=periods, tz_name=tz)
    except SalesError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


@router.get("/report")
async def read_report(
    grain: str = "month",
    periods: int | None = Query(default=None, ge=1, le=sales.MAX_SPAN),
    # The browser's own zone, so the weeks break where the shop's weeks do.
    # Optional, and UTC without it — a report in the wrong zone is still a
    # report, and it says which zone it used.
    tz: str | None = None,
    open_limit: int = Query(default=100, ge=0, le=500),
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    report = await _report(session, grain, periods, tz)
    report["open_orders"] = (
        await sales.open_orders(session, limit=open_limit) if open_limit else []
    )
    return report


@router.get("/report.csv")
async def download_report(
    grain: str = "month",
    periods: int | None = Query(default=None, ge=1, le=sales.MAX_SPAN),
    tz: str | None = None,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The same table, for a spreadsheet.

    A sales report ends up in somebody's accountant's inbox, and retyping a
    screen into a spreadsheet is how a figure changes on the way.
    """
    report = await _report(session, grain, periods, tz)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Period",
            "Starts",
            "Ends",
            "Orders",
            "Sales",
            "Items",
            "Shipping",
            "Tax",
            "Discount",
            "Fees",
            "Postage",
            "Net",
            "Processed orders",
            "Processed sales",
            "In progress orders",
            "In progress sales",
            "Invoiced orders",
            "Invoiced total",
            "Cancelled orders",
        ]
    )
    for period in [*report["periods"], _total_row(report)]:
        writer.writerow(
            [
                period["label"],
                period["start"],
                period["end"],
                period["all_orders"],
                period["all"]["revenue"],
                period["all"]["items_total"],
                period["all"]["shipping_total"],
                period["all"]["tax_total"],
                period["all"]["discount_total"],
                _fees(period["all"]),
                period["all"]["label_cost"],
                period["all"]["net"],
                period["done_orders"],
                period["done"]["revenue"],
                period["live_orders"],
                period["live"]["revenue"],
                period["invoiced_orders"],
                period["invoiced_total"],
                period["cancelled_orders"],
            ]
        )
    name = f"printflow-sales-{report['grain']}-{report['from']}-to-{report['to']}.csv"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _fees(figures: dict[str, str]) -> str:
    """The three fee columns as one, which is how a spreadsheet wants them."""
    total = sum(
        (Decimal(figures[name]) for name in ("etsy_fees", "marketing_fees", "processing_fees")),
        Decimal("0.00"),
    )
    return str(total)


def _total_row(report: dict[str, Any]) -> dict[str, Any]:
    """The totals, shaped like a period so the writer has one loop."""
    totals = report["totals"]
    return {
        "label": "Total",
        "start": report["from"],
        "end": report["to"],
        **{key: totals[key] for key in ("all", "done", "live")},
        **{
            f"{key}_orders": totals[f"{key}_orders"]
            for key in ("all", "done", "live", "invoiced", "cancelled")
        },
        "invoiced_total": totals["invoiced_total"],
    }
