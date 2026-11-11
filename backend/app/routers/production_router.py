"""What still has to be made. One read, and the same list as a spreadsheet.

Nothing here writes anything: the list is a question about orders and plates
that already exist. Sending the planned plates to Bambuddy is a different
thing, and it already has a button of its own on the print-jobs router.
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

    One line per thing to make and how many of it — the list somebody carries
    to a machine, not a report about orders.
    """
    found = await production.to_make(session)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "Code",
            "Item",
            "Variation",
            "To make",
            "Covered",
            "Needed",
            "Per plate",
            "Plates",
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
                row["units_outstanding"],
                row["units_covered"],
                row["units_needed"],
                row["units_per_plate"] or "",
                row["plates_outstanding"] or "",
                _day(row["waiting_since"]),
                row["stage_label"],
            ]
        )

    for line in found["unmatched"]:
        writer.writerow(
            [
                line["sku"] or "",
                line["title"] or "",
                "",
                line["units"],
                "",
                line["units"],
                "",
                "",
                _day(line["waiting_since"]),
                "No product matched",
            ]
        )

    for waiting in found["assembly"]:
        writer.writerow(
            [
                waiting["sku"] or "",
                waiting["name"] or "",
                "",
                waiting["ready"],
                "",
                waiting["quantity"],
                "",
                "",
                _day(waiting["waiting_since"]),
                "Assemble"
                if not waiting["waiting_on_parts"]
                else f"Assemble ({waiting['waiting_on_parts']} still waiting on parts)",
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
