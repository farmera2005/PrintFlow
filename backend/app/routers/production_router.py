"""What still has to be made. One read, and the same list as a spreadsheet.

Nothing here writes anything, and nothing here starts anything: the list is a
question about orders and work that already exist. Acting on it — which file,
which machine, what gets sent where — belongs to the print-jobs router, and is
deliberately not reachable from this one.
"""

from __future__ import annotations

import csv
import io

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..models import User
from ..services import production

router = APIRouter(prefix="/api", tags=["production"])


class PriorityKey(BaseModel):
    """One thing, named the way the list names it."""

    product_id: uuid.UUID
    variation_id: uuid.UUID | None = None


class PriorityOrder(BaseModel):
    """The whole priority list, in the order somebody just dragged it into.

    A cap, because this arrives from a browser: a priority list is a handful
    of things a person reordered by hand, and anything approaching this many
    is a mistake or an attack rather than a plan.
    """

    items: list[PriorityKey] = Field(default_factory=list, max_length=200)


@router.get("/production")
async def read_production(
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await production.to_make(session)


@router.put("/production/priority")
async def set_priority(
    body: PriorityOrder,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace what goes first, and hand back the list as it now reads.

    The whole list in, the whole screen out: a drag changes two sections at
    once, and re-rendering from what the server actually stored is the only
    version of this that cannot drift from it.
    """
    try:
        await production.set_priority(
            session,
            [(key.product_id, key.variation_id) for key in body.items],
            by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return await production.to_make(session)


@router.get("/production.csv")
async def download_production(
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The same list, for a clipboard on the shop floor.

    One line per thing and how many of it. `From stock` says how much of that
    quantity is a walk to a shelf rather than a machine — a note on the row,
    the same as on the screen, and not a second list.
    """
    found = await production.to_make(session)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "First",
            "Code",
            "Item",
            "Variation",
            "Quantity",
            "From stock",
            "For",
            "Waiting since",
            "Status",
        ]
    )
    # Pinned things first, in the shop's order, the same as on the screen —
    # a printed sheet that reordered itself would be a different list. The
    # number is in a column of its own so the order survives a sort in a
    # spreadsheet, which is the first thing anybody does to a CSV.
    for place, row in enumerate(found["priority"], start=1):
        if row["units"] <= 0:
            continue
        writer.writerow(
            [
                place,
                row["sku"],
                row["name"],
                row["variation_label"] or "",
                row["units"],
                row["units_from_stock"],
                ", ".join(row["bundles"]),
                _day(row["waiting_since"]),
                "To make" if row["can_start"] else "Cannot be made yet",
            ]
        )

    for row in found["items"]:
        writer.writerow(
            [
                "",
                row["sku"],
                row["name"],
                row["variation_label"] or "",
                row["units"],
                row["units_from_stock"],
                ", ".join(row["bundles"]),
                _day(row["waiting_since"]),
                "To make" if row["can_start"] else "Cannot be made yet",
            ]
        )

    for row in found["in_production"]:
        writer.writerow(
            [
                "",
                row["sku"],
                row["name"],
                row["variation_label"] or "",
                row["units"],
                0,
                "",
                _day(row["started_at"]),
                f"Being made ({row['units_made']} of {row['units']} done)",
            ]
        )

    for line in found["unmatched"]:
        writer.writerow(
            [
                "",
                line["sku"] or "",
                line["title"] or "",
                "",
                line["units"],
                0,
                "",
                _day(line["waiting_since"]),
                "No product matched",
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
