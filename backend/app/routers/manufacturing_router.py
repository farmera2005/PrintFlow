"""Made-items sheets: drafts here, QuickBooks Purchases when posted.

Every write to QuickBooks in this file is behind an explicit POST from a signed-
in operator. Nothing here is reachable from a scheduled job.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user
from ..db import get_session
from ..integrations.base import IntegrationError
from ..models import (
    SHEET_DRAFT,
    SHEET_POSTED,
    MadeSheet,
    MadeSheetLine,
    Product,
    User,
)
from ..services import audit, manufacturing, settings_store
from ..services.credentials import IntegrationNotConfigured
from ..services.manufacturing import SheetError

router = APIRouter(prefix="/api/manufacturing", tags=["manufacturing"])


def _serialize_line(line: MadeSheetLine) -> dict[str, Any]:
    return {
        "id": line.id,
        "product_id": line.product_id,
        # `sku` is the label the UI shows, whichever kind of line it is.
        "sku": line.item_label,
        "name": line.item_name,
        "fulfillment": line.product.fulfillment if line.product else None,
        "qbo_item_id": line.item_id,
        "source": "product" if line.product else "quickbooks",
        "has_bom": bool(line.bom_lines),
        "quantity": line.quantity,
        "unit_cost": str(manufacturing.money(line.unit_cost)),
        "cost_from_bom": line.cost_from_bom,
        "amount": str(manufacturing.money(Decimal(str(line.unit_cost)) * line.quantity)),
    }


def _serialize(sheet: MadeSheet) -> dict[str, Any]:
    return {
        "id": sheet.id,
        "reference": sheet.reference,
        "made_on": sheet.made_on,
        "memo": sheet.memo,
        "status": sheet.status,
        "qbo_purchase_id": sheet.qbo_purchase_id,
        "qbo_doc_number": sheet.qbo_doc_number,
        "posted_at": sheet.posted_at,
        "posted_by": sheet.posted_by,
        "voided_at": sheet.voided_at,
        "voided_by": sheet.voided_by,
        "created_at": sheet.created_at,
        "lines": [_serialize_line(line) for line in sheet.lines],
        "total": str(
            manufacturing.money(
                sum(
                    (Decimal(str(line.unit_cost)) * line.quantity for line in sheet.lines),
                    Decimal("0"),
                )
            )
        ),
    }


async def _load(session: AsyncSession, sheet_id: uuid.UUID) -> MadeSheet:
    sheet = await manufacturing.get_sheet(session, sheet_id)
    if sheet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sheet not found")
    return sheet


def _editable(sheet: MadeSheet) -> None:
    try:
        manufacturing.assert_editable(sheet)
    except SheetError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _parse_cost(raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unit cost is not a number") from exc
    if value < 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unit cost cannot be negative")
    return manufacturing.money(value)


# --------------------------------------------------------------------------
# Posting settings
# --------------------------------------------------------------------------


class ManufacturingSettingsRequest(BaseModel):
    account_id: str | None = None
    account_name: str | None = None
    payment_type: str | None = None
    vendor_id: str | None = None
    vendor_name: str | None = None
    doc_number_prefix: str | None = None


@router.get("/settings")
async def read_settings(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    return {
        "settings": await settings_store.get_manufacturing_settings(session),
        "payment_types": list(settings_store.PAYMENT_TYPES),
    }


@router.put("/settings")
async def write_settings(
    body: ManufacturingSettingsRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    values = body.model_dump(exclude_unset=True)
    saved = await settings_store.set_manufacturing_settings(session, values)
    await audit.record(
        session,
        entity_type="settings",
        entity_id=None,
        action="manufacturing_settings_changed",
        # The account is the one that decides where the money lands, so it is
        # worth having in the log by name rather than just by id.
        detail={"account": saved.get("account_name"), "payment_type": saved.get("payment_type")},
        actor=user.username,
    )
    await session.commit()
    return {"settings": saved}


# --------------------------------------------------------------------------
# Sheets
# --------------------------------------------------------------------------


@router.get("/sheets")
async def list_sheets(
    limit: int = 50,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rows = (
        (
            await session.execute(
                select(MadeSheet)
                .order_by(MadeSheet.created_at.desc())
                .limit(max(1, min(limit, 200)))
            )
        )
        .scalars()
        .all()
    )
    return {"sheets": [_serialize(sheet) for sheet in rows]}


class SheetRequest(BaseModel):
    made_on: datetime | None = None
    memo: str | None = None
    reference: str | None = None


@router.post("/sheets")
async def create_sheet(
    body: SheetRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = MadeSheet(
        reference=(body.reference or "").strip() or await manufacturing.next_reference(session),
        made_on=body.made_on or datetime.now(timezone.utc),
        memo=(body.memo or "").strip() or None,
        status=SHEET_DRAFT,
        idempotency_key=uuid.uuid4(),
    )
    session.add(sheet)
    await session.flush()
    await audit.record(
        session,
        entity_type="made_sheet",
        entity_id=sheet.id,
        action="created",
        detail={"reference": sheet.reference},
        actor=user.username,
    )
    sheet_id = sheet.id
    await session.commit()
    return _serialize(await _load(session, sheet_id))


@router.get("/sheets/{sheet_id}")
async def get_sheet(
    sheet_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return _serialize(await _load(session, sheet_id))


@router.patch("/sheets/{sheet_id}")
async def update_sheet(
    sheet_id: uuid.UUID,
    body: SheetRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = await _load(session, sheet_id)
    _editable(sheet)
    if body.made_on is not None:
        sheet.made_on = body.made_on
    if body.memo is not None:
        sheet.memo = body.memo.strip() or None
    if body.reference is not None and body.reference.strip():
        sheet.reference = body.reference.strip()
    await session.commit()
    return _serialize(await _load(session, sheet_id))


@router.delete("/sheets/{sheet_id}")
async def delete_sheet(
    sheet_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = await _load(session, sheet_id)
    if sheet.status == SHEET_POSTED:
        # Deleting the record would leave a Purchase in QuickBooks with nothing
        # in PrintFlow explaining where it came from.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This sheet is posted to QuickBooks. Void it first, then it can be deleted.",
        )
    await audit.record(
        session,
        entity_type="made_sheet",
        entity_id=sheet.id,
        action="deleted",
        detail={"reference": sheet.reference, "status": sheet.status},
        actor=user.username,
    )
    await session.delete(sheet)
    await session.commit()
    return {"deleted": True}


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------


class LineRequest(BaseModel):
    """Name a PrintFlow product or a QuickBooks item — one of the two."""

    product_id: uuid.UUID | None = None
    qbo_item_id: str | None = None
    qbo_item_name: str | None = None
    quantity: int = Field(gt=0)
    unit_cost: str | float | int | None = None


@router.post("/sheets/{sheet_id}/lines")
async def add_line(
    sheet_id: uuid.UUID,
    body: LineRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = await _load(session, sheet_id)
    _editable(sheet)

    if bool(body.product_id) == bool(body.qbo_item_id):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Give either a product or a QuickBooks item, not both and not neither.",
        )

    product: Product | None = None
    qbo_item_id: str | None = None

    if body.product_id:
        product = await manufacturing.loaded_product(session, body.product_id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    else:
        qbo_item_id = str(body.qbo_item_id)
        # If a product already maps to this item, store the line against the
        # product. Otherwise picking "Dragon Egg" from the QuickBooks list would
        # skip its BOM and consume no components, so the same physical act would
        # post two different ways depending on which picker was used.
        product = await manufacturing.product_for_item(session, qbo_item_id)
        if product is not None:
            qbo_item_id = None

    from_bom = False
    if body.unit_cost is None:
        # Prefill, and remember that it was a suggestion so a later change can
        # refresh it without clobbering a typed figure.
        suggested = (
            await manufacturing.suggest_unit_cost(session, product)
            if product is not None
            else await manufacturing.qbo_item_cost(session, str(qbo_item_id))
        )
        cost = suggested if suggested is not None else Decimal("0.00")
        from_bom = suggested is not None
    else:
        cost = _parse_cost(body.unit_cost)

    # Read what the error message needs before committing: a rollback expires
    # every loaded object, and reading one back afterwards fails rather than
    # reloading, which would hide the real error behind a second one.
    label = product.sku if product is not None else (body.qbo_item_name or "That item")

    line = MadeSheetLine(
        sheet_id=sheet.id,
        product_id=product.id if product is not None else None,
        qbo_item_id=qbo_item_id,
        qbo_item_name=(body.qbo_item_name or None) if qbo_item_id else None,
        quantity=body.quantity,
        unit_cost=cost,
        cost_from_bom=from_bom,
    )
    session.add(line)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{label} is already on this sheet — change its quantity instead.",
        ) from exc

    return _serialize(await _load(session, sheet_id))


class LineUpdateRequest(BaseModel):
    quantity: int | None = Field(default=None, gt=0)
    unit_cost: str | float | int | None = None


@router.patch("/sheets/{sheet_id}/lines/{line_id}")
async def update_line(
    sheet_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LineUpdateRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = await _load(session, sheet_id)
    _editable(sheet)
    line = await session.get(MadeSheetLine, line_id)
    if line is None or line.sheet_id != sheet.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Line not found")

    if body.quantity is not None:
        line.quantity = body.quantity
    if body.unit_cost is not None:
        line.unit_cost = _parse_cost(body.unit_cost)
        # Typed over: stop treating it as the BOM's answer.
        line.cost_from_bom = False
    await session.commit()

    refreshed = await _load(session, sheet_id)
    return _serialize(refreshed)


@router.delete("/sheets/{sheet_id}/lines/{line_id}")
async def delete_line(
    sheet_id: uuid.UUID,
    line_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = await _load(session, sheet_id)
    _editable(sheet)
    line = await session.get(MadeSheetLine, line_id)
    if line is None or line.sheet_id != sheet.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Line not found")
    await session.delete(line)
    await session.commit()

    refreshed = await _load(session, sheet_id)
    return _serialize(refreshed)


# --------------------------------------------------------------------------
# Preview, post, void
# --------------------------------------------------------------------------


@router.get("/sheets/{sheet_id}/preview")
async def preview_sheet(
    sheet_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What posting would do to the books. Reads only."""
    sheet = await _load(session, sheet_id)
    try:
        return await manufacturing.preview(session, sheet)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/sheets/{sheet_id}/post")
async def post_sheet(
    sheet_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Write the Purchase to QuickBooks. The only write in the app."""
    sheet = await _load(session, sheet_id)
    try:
        await manufacturing.post(session, sheet, actor=user.username)
    except IntegrationNotConfigured as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except SheetError as exc:
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except IntegrationError as exc:  # pragma: no cover - defensive
        await session.rollback()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await session.commit()
    return _serialize(await _load(session, sheet_id))


@router.post("/sheets/{sheet_id}/void")
async def void_sheet(
    sheet_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    sheet = await _load(session, sheet_id)
    try:
        await manufacturing.void(session, sheet, actor=user.username)
    except IntegrationNotConfigured as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except SheetError as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return _serialize(await _load(session, sheet_id))
