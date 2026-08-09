"""Flow board, order detail drawer, manual overrides, and label creation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import require_user
from ..db import get_session
from ..integrations.base import IntegrationError
from ..models import (
    LINE_CANCELLED,
    ORDER_STATUSES,
    LINE_PRINTED,
    LINE_READY,
    ORDER_READY_TO_SHIP,
    Order,
    OrderLine,
    Product,
    User,
)
from ..services import audit, board, intake, shipping
from ..services.credentials import IntegrationNotConfigured
from ..services.state import ALLOWED_LINE_OVERRIDES, recompute_order

router = APIRouter(prefix="/api", tags=["orders"])


async def _get_order(session: AsyncSession, order_id: uuid.UUID) -> Order:
    order = (
        await session.execute(
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.lines).selectinload(OrderLine.print_jobs))
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return order


async def _get_line(
    session: AsyncSession, order_id: uuid.UUID, line_id: uuid.UUID
) -> OrderLine:
    line = await session.get(OrderLine, line_id)
    if line is None or line.order_id != order_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order line not found")
    return line


@router.get("/board")
async def get_board(
    limit_per_column: int = 100,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return await board.build_board(
        session, limit_per_column=max(1, min(limit_per_column, 500))
    )


@router.get("/orders")
async def list_orders(
    q: str = "",
    status_filter: str | None = None,
    limit: int = 100,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Every order, whatever its status.

    The board only draws the five live columns, so a cancelled order — or a
    shipped one from last month — is invisible there. This is the way back to
    it.
    """
    stmt = select(Order).options(
        selectinload(Order.lines).selectinload(OrderLine.print_jobs),
        selectinload(Order.lines).selectinload(OrderLine.product),
    )
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            Order.order_number.ilike(needle) | Order.buyer_name.ilike(needle)
        )
    if status_filter:
        if status_filter not in ORDER_STATUSES:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"status_filter must be one of {', '.join(ORDER_STATUSES)}",
            )
        stmt = stmt.where(Order.status == status_filter)
    stmt = stmt.order_by(Order.placed_at.desc().nullslast()).limit(max(1, min(limit, 500)))
    orders = (await session.execute(stmt)).scalars().all()

    counts = dict(
        (
            await session.execute(select(Order.status, func.count()).group_by(Order.status))
        ).all()
    )
    return {
        "orders": [board.order_card(order) for order in orders],
        "counts": {name: counts.get(name, 0) for name in ORDER_STATUSES},
        "total": sum(counts.values()),
    }


@router.get("/orders/{order_id}")
async def get_order(
    order_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    detail = await board.load_order_detail(session, order_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    return detail


@router.get("/orders/{order_id}/raw")
async def get_order_raw(
    order_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    order = await _get_order(session, order_id)
    return {"raw": order.raw}


@router.post("/orders/{order_id}/reprocess")
async def reprocess_order(
    order_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Re-run the intake pipeline for one order. Idempotent (§6)."""
    order = await _get_order(session, order_id)
    await intake.process_order(session, order)
    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="reprocess",
        actor=user.username,
    )
    await session.commit()
    return await board.load_order_detail(session, order_id)


# --------------------------------------------------------------------------
# Line-level actions
# --------------------------------------------------------------------------


@router.post("/orders/{order_id}/reset-matching")
async def reset_matching(
    order_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Unmatch everything and run intake again from scratch.

    The fix after the catalogue has changed underneath an order — a listing
    linked, variations added, a BOM corrected. Re-running intake alone keeps
    whatever each line already matched; this throws that away first.
    """
    order = await _get_order(session, order_id)
    totals = await intake.reset_order_matching(session, order)
    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="reset_matching",
        detail=totals,
        actor=user.username,
    )
    await session.commit()
    detail = await board.load_order_detail(session, order_id)
    detail.update(totals)
    return detail


@router.post("/orders/{order_id}/lines/{line_id}/unmatch")
async def unmatch_line(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Let go of this line's product so a different one can be picked."""
    line = await _get_line(session, order_id, line_id)
    if line.parent_line_id is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This is a bundle component. Change the item it came from instead.",
        )
    result = await intake.clear_line_match(session, line)
    order = await _get_order(session, order_id)
    await recompute_order(session, order)
    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="unmatch",
        detail=result,
        actor=user.username,
    )
    await session.commit()
    detail = await board.load_order_detail(session, order_id)
    detail.update(result)
    return detail


class LinkProductRequest(BaseModel):
    product_id: uuid.UUID
    # Etsy does not require a SKU, so some listings have nothing to match on.
    # Remembering the listing turns a one-off fix into a rule.
    remember: bool = False
    remember_scope: str = Field(default="listing", pattern="^(listing|variant)$")


@router.post("/orders/{order_id}/lines/{line_id}/link-product")
async def link_product(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    body: LinkProductRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fix path for an unmatched SKU: link a product and re-run intake (§4.1)."""
    line = await _get_line(session, order_id, line_id)
    product = await session.get(Product, body.product_id)
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    await intake.relink_line(session, line, product)

    remembered = None
    also_fixed = 0
    if body.remember:
        link = await intake.remember_etsy_link(
            session, line, product, body.remember_scope
        )
        if link is not None:
            remembered = link.etsy_product_id or link.etsy_listing_id
            also_fixed = await intake.apply_etsy_link(session, link)

    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="link_product",
        detail={
            "sku_raw": line.sku_raw,
            "linked_sku": product.sku,
            "etsy_listing_id": line.etsy_listing_id,
            "remembered": remembered,
            "remember_scope": body.remember_scope if remembered else None,
            "also_fixed": also_fixed,
        },
        actor=user.username,
    )
    await session.commit()
    detail = await board.load_order_detail(session, order_id)
    detail["also_fixed"] = also_fixed
    return detail


class OverrideRequest(BaseModel):
    action: str = Field(pattern="^(mark_printed|mark_ready|cancel|clear)$")
    reason: str | None = None


@router.post("/orders/{order_id}/lines/{line_id}/override")
async def override_line(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    body: OverrideRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Manual overrides from the card menu. Every one writes an audit row (§5)."""
    line = await _get_line(session, order_id, line_id)
    order = await _get_order(session, order_id)

    mapping = {
        "mark_printed": LINE_PRINTED,
        "mark_ready": LINE_READY,
        "cancel": LINE_CANCELLED,
        "clear": None,
    }
    new_override = mapping[body.action]
    if new_override is not None and new_override not in ALLOWED_LINE_OVERRIDES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unsupported override")

    line.override_state = new_override
    if body.action == "cancel":
        # Cancelling releases this line's soft stock reservation.
        line.state = LINE_CANCELLED
    await session.flush()
    await recompute_order(session, order)
    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action=f"override_{body.action}",
        detail={"reason": body.reason, "sku": line.sku_raw},
        actor=user.username,
    )
    await session.commit()
    return await board.load_order_detail(session, order_id)


class ForcePrintRequest(BaseModel):
    force_print: bool = True
    reason: str | None = None


@router.post("/orders/{order_id}/lines/{line_id}/force-print")
async def force_print(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    body: ForcePrintRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """"Skip stock, print anyway" — re-runs decisioning for this line."""
    line = await _get_line(session, order_id, line_id)
    product = await session.get(Product, line.product_id) if line.product_id else None
    if body.force_print and (product is None or product.fulfillment != "printed"):
        # A stocked SKU has no print path, so "print anyway" would strand the line.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only lines for a 'printed' product can skip the stock check.",
        )
    line.force_print = body.force_print
    await session.flush()
    await intake.redecide_line(session, line)
    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="force_print" if body.force_print else "unforce_print",
        detail={"reason": body.reason},
        actor=user.username,
    )
    await session.commit()
    return await board.load_order_detail(session, order_id)


class AssembleRequest(BaseModel):
    assembled: bool = True


@router.post("/orders/{order_id}/lines/{line_id}/assemble")
async def assemble_bundle(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    body: AssembleRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Assembly check-off, one per bundle line (§5)."""
    line = await _get_line(session, order_id, line_id)
    has_children = (
        await session.execute(
            select(OrderLine.id).where(OrderLine.parent_line_id == line.id).limit(1)
        )
    ).first()
    if not has_children:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only bundle lines have an assembly step."
        )
    line.assembled_at = datetime.now(timezone.utc) if body.assembled else None
    await session.flush()
    order = await _get_order(session, order_id)
    await recompute_order(session, order)
    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="assembled" if body.assembled else "assembly_undone",
        actor=user.username,
    )
    await session.commit()
    return await board.load_order_detail(session, order_id)


# --------------------------------------------------------------------------
# ShipStation
# --------------------------------------------------------------------------


@router.post("/orders/{order_id}/match-shipstation")
async def match_shipstation(
    order_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    from ..integrations import shipstation as ss_api

    order = await _get_order(session, order_id)
    try:
        client = await ss_api.client_for(session)
        found = await client.find_order_by_number(order.order_number)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    order.shipstation_attempts += 1
    order.shipstation_last_attempt_at = datetime.now(timezone.utc)
    if found and found.get("orderId"):
        order.shipstation_order_id = int(found["orderId"])
    await session.commit()
    return {
        "matched": order.shipstation_order_id is not None,
        "shipstation_order_id": order.shipstation_order_id,
    }


@router.get("/orders/{order_id}/label-context")
async def label_context(
    order_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    order = await _get_order(session, order_id)
    try:
        return await shipping.label_context(session, order)
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


class CreateLabelRequest(BaseModel):
    carrier_code: str
    service_code: str
    package_code: str = "package"
    weight_value: float = Field(gt=0)
    weight_units: str = "ounces"
    confirmation: str | None = None
    test_label: bool = False
    allow_not_ready: bool = False


@router.post("/orders/{order_id}/label")
async def create_label(
    order_id: uuid.UUID,
    body: CreateLabelRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Buy a shipping label. Always user-triggered — never automatic (§4.4)."""
    order = await _get_order(session, order_id)
    if order.status != ORDER_READY_TO_SHIP and not body.allow_not_ready:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Order is '{order.status}', not ready to ship. "
            "Confirm the override to create a label anyway.",
        )
    try:
        result = await shipping.create_label(
            session,
            order,
            carrier_code=body.carrier_code,
            service_code=body.service_code,
            package_code=body.package_code,
            weight_value=body.weight_value,
            weight_units=body.weight_units,
            confirmation=body.confirmation,
            test_label=body.test_label,
        )
    except shipping.LabelError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="label_created",
        detail={
            "carrier_code": body.carrier_code,
            "service_code": body.service_code,
            "tracking_number": result["tracking_number"],
            "forced": body.allow_not_ready,
        },
        actor=user.username,
    )
    await session.commit()
    return result


@router.get("/orders/{order_id}/label.pdf")
async def download_label(
    order_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await _get_order(session, order_id)
    if not order.label_pdf:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No label stored for this order")
    return Response(
        content=bytes(order.label_pdf),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="label-{order.order_number}.pdf"'
        },
    )
