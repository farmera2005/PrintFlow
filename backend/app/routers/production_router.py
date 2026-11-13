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

from fastapi import APIRouter, Depends, HTTPException, Query
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


def _columns(raw: str | None) -> list[str] | None:
    """Board columns off the query string: `?columns=new,assembly`.

    Comma-separated rather than repeated, because this also has to live in
    the href of a download link, where one short parameter is easier to read
    and to copy than four.
    """
    if raw is None:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


@router.get("/production")
async def read_production(
    columns: str | None = Query(
        None,
        description="Board columns to show, comma-separated. All open columns "
        "when left out.",
        max_length=200,
    ),
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    try:
        return await production.to_make(session, columns=_columns(columns))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/production/priority")
async def set_priority(
    body: PriorityOrder,
    columns: str | None = Query(None, max_length=200),
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Replace what goes first, and hand back the list as it now reads.

    The whole list in, the whole screen out: a drag changes two sections at
    once, and re-rendering from what the server actually stored is the only
    version of this that cannot drift from it. It takes the screen's
    `columns` so that what comes back is the screen the person is looking
    at — answering a drag with an unfiltered list would silently undo a
    filter they set a moment ago.
    """
    try:
        # Both arguments checked before anything is written, so a mistyped
        # filter cannot leave the priority list changed and the caller
        # looking at an error.
        showing = production.resolve_columns(_columns(columns))
        await production.set_priority(
            session,
            [(key.product_id, key.variation_id) for key in body.items],
            by=user.username,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await session.commit()
    return await production.to_make(session, columns=showing)


@router.get("/production.csv")
async def download_production(
    columns: str | None = Query(
        None,
        description="Board columns to include, comma-separated. All open "
        "columns when left out.",
        max_length=200,
    ),
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The same list, for a clipboard on the shop floor.

    One line per thing and how many of it. `From stock` says how much of that
    quantity is a walk to a shelf rather than a machine — a note on the row,
    the same as on the screen, and not a second list.

    It takes the same `columns` as the screen, and the download link carries
    whatever is on screen at the time: a sheet that silently held more than
    the list it was printed from is a sheet somebody works the wrong day from.
    """
    try:
        found = await production.to_make(session, columns=_columns(columns))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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

    # The columns in the name, when it is not the whole shop: two downloads
    # on the same day are two different lists, and a Downloads folder that
    # silently overwrote one with the other would be a nasty surprise.
    narrowed = [row["key"] for row in found["columns"] if row["showing"]]
    stamp = found["generated_at"].strftime("%Y-%m-%d")
    if len(narrowed) != len(found["columns"]):
        stamp = f"{stamp}-{'-'.join(name.replace('_', '-') for name in narrowed)}"
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="printflow-to-make-{stamp}.csv"'
        },
    )


def _day(when) -> str:
    return when.strftime("%Y-%m-%d") if when else ""
