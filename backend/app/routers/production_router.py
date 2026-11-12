"""What still has to be made. One read, and the same list as a spreadsheet.

Nothing here writes anything, and nothing here starts anything: the list is a
question about orders and work that already exist. Acting on it — which file,
which machine, what gets sent where — belongs to the print-jobs router, and is
deliberately not reachable from this one.
"""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..models import User
from ..services import production

router = APIRouter(prefix="/api", tags=["production"])


@router.get("/production")
async def read_production(
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await production.to_make(session)


@router.get("/production.csv")
async def download_production(
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The same list, for a clipboard on the shop floor.

    One line per thing and how many of it — what to make, what is already
    being made, and what to fetch.
    """
    found = await production.to_make(session)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Code",
            "Item",
            "Variation",
            "Quantity",
            "Needed",
            "Waiting since",
            "Status",
        ]
    )
    for row in found["items"]:
        writer.writerow(
            [
                row["sku"],
                row["name"],
                row["variation_label"] or "",
                row["units_to_make"],
                row["units_needed"],
                _day(row["waiting_since"]),
                "To make" if row["can_start"] else "Cannot be made yet",
            ]
        )

    for row in found["in_production"]:
        writer.writerow(
            [
                row["sku"],
                row["name"],
                row["variation_label"] or "",
                row["units"],
                row["units"],
                _day(row["started_at"]),
                f"Being made ({row['units_made']} of {row['units']} done)",
            ]
        )

    for line in found["unmatched"]:
        writer.writerow(
            [
                line["sku"] or "",
                line["title"] or "",
                "",
                line["units"],
                line["units"],
                _day(line["waiting_since"]),
                "No product matched",
            ]
        )

    for part in found["gather"]:
        writer.writerow(
            [
                part["sku"] or "",
                part["name"] or "",
                part["variation_label"] or "",
                part["units"],
                part["units"] + part["units_waiting"],
                _day(part["waiting_since"]),
                "Gather for " + ", ".join(part["bundles"])
                if part["bundles"]
                else "Gather from stock",
            ]
        )

    stamp = found["generated_at"].strftime("%Y-%m-%d")
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="printflow-to-make-{stamp}.csv"'
        },
    )


def _day(when) -> str:
    return when.strftime("%Y-%m-%d") if when else ""
