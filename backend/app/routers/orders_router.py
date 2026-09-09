"""Flow board, order detail drawer, manual overrides, and label creation."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
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
    ORDER_CANCELLED,
    ORDER_COMPLETE,
    ORDER_STATUSES,
    LINE_PRINTED,
    LINE_READY,
    ORDER_READY_TO_SHIP,
    Order,
    OrderLine,
    Product,
    User,
)
from ..services import audit, board, books, finance, intake, shipping
from ..services.books import BooksError
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


class OrderStatusRequest(BaseModel):
    status: str
    note: str | None = Field(default=None, max_length=500)


@router.put("/orders/{order_id}/status")
async def set_order_status(
    order_id: uuid.UUID,
    body: OrderStatusRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Move an order to a column. The only thing that ever moves one.

    The status is not just a label: it decides what the lines are. Cancelling
    cancels them, which releases their stock and keeps their plates off the
    printers; Shipped carries them to shipped. Moving the card back recomputes
    all of it, because none of it is written down separately.
    """
    if body.status not in ORDER_STATUSES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"status must be one of {', '.join(ORDER_STATUSES)}",
        )

    order = await _get_order(session, order_id)
    was = order.status
    order.status = body.status
    order.status_note = (body.note or "").strip() or None

    # The 48-hour clock that takes a finished card off the board runs from when
    # it entered the column, so entering it starts the clock and leaving it
    # throws the clock away. Dragging a card out and back gives it two fresh
    # days, which is the point: somebody put it back for a reason.
    if body.status == ORDER_COMPLETE and was != ORDER_COMPLETE:
        order.completed_at = datetime.now(timezone.utc)
    elif body.status != ORDER_COMPLETE:
        order.completed_at = None

    # Coming back out of Cancelled, the lines have to come with it. A line
    # cancelled by hand keeps its own override otherwise, so the order would sit
    # in a live column with nothing on it — still cancelled in every way that
    # shows, just not in the column heading.
    restored = 0
    if was == ORDER_CANCELLED and body.status != ORDER_CANCELLED:
        for line in order.lines:
            if line.override_state == LINE_CANCELLED:
                line.override_state = None
                restored += 1

    await session.flush()
    # Not to work out the status — to bring the lines into line with it.
    await recompute_order(session, order)

    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="set_status",
        detail={
            "from": was,
            "to": order.status,
            "note": order.status_note,
            "lines_restored": restored,
        },
        actor=user.username,
    )
    await session.commit()
    detail = await board.load_order_detail(session, order_id)
    detail["lines_restored"] = restored
    return detail


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
    # Nested, not merged: the order payload has a `lines` key of its own — the
    # line tree the drawer renders — and merging a count called `lines` over it
    # replaced the array with an integer and blanked the screen.
    detail["reset"] = totals
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
    detail["unmatched"] = result
    return detail


class LinkProductRequest(BaseModel):
    product_id: uuid.UUID
    # Neither channel requires a SKU, so some listings have nothing to match
    # on. Remembering the listing — or the Wix catalogue item — turns a one-off
    # fix into a rule.
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
        link = await intake.remember_channel_link(
            session, line, product, body.remember_scope
        )
        if link is not None:
            remembered = intake.link_identity(link)
            also_fixed = await intake.apply_channel_link(session, link)

    await audit.record(
        session,
        entity_type="order_line",
        entity_id=line.id,
        action="link_product",
        detail={
            "sku_raw": line.sku_raw,
            "linked_sku": product.sku,
            "etsy_listing_id": line.etsy_listing_id,
            "wix_catalog_item_id": line.wix_catalog_item_id,
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
    # Marking a line printed says those units exist and are spoken for, so
    # QuickBooks stops counting them. Cancelling one that was already booked
    # puts them back. Both directions live in sync_order_stock, which is why
    # this is one call rather than a branch per action.
    booked = await books.sync_order_stock(session, order, actor=user.username)
    await session.commit()
    detail = await board.load_order_detail(session, order_id)
    detail["books"] = booked
    return detail


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
    """Assembly check-off, one per bundle line (§5).

    Ticking it also takes the bundle's components out of QuickBooks stock —
    they are inside the assembled item now, and the ones pulled from the shelf
    rather than printed had never been booked out by anything. Unticking puts
    back exactly what the assembly took. See services/books.book_assembly.
    """
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
    booked = (
        await books.book_assembly(session, line, actor=user.username, order=order)
        if body.assembled
        else await books.unbook_assembly(session, line, actor=user.username)
    )
    await session.commit()
    detail = await board.load_order_detail(session, order_id)
    detail["books"] = booked
    return detail


# --------------------------------------------------------------------------
# QuickBooks: stock out when it is printed, an invoice when it is sold
# --------------------------------------------------------------------------


@router.post("/orders/{order_id}/lines/{line_id}/stock-removal")
async def remove_line_stock(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take this line's units out of QuickBooks stock, or try again after a failure.

    The removal normally happens on its own when the line is printed. This is
    the way back in when it could not: QuickBooks was down, or an account had
    not been chosen yet. Booking twice is impossible — the line records that it
    has already been done.
    """
    line = await _get_line(session, order_id, line_id)
    order = await _get_order(session, order_id)
    result = await books.remove_stock(session, line, actor=user.username, order=order)
    await session.commit()
    if result["failed"]:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, result["reason"])
    detail = await board.load_order_detail(session, order_id)
    detail["books"] = [{"line_id": str(line_id), **result}]
    return detail


@router.delete("/orders/{order_id}/lines/{line_id}/stock-removal")
async def restore_line_stock(
    order_id: uuid.UUID,
    line_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Put this line's units back into QuickBooks stock.

    Cancelling a line does this on its own, because stock that never left the
    shop should not stay gone. This is the other case: somebody marked a line
    printed that was not, and clearing the override cannot be trusted to mean
    "undo the books" — it is just as often a tidy-up after a print that really
    did finish. So the reversal is its own button, and it is deliberate.
    """
    line = await _get_line(session, order_id, line_id)
    result = await books.restore_stock(session, line, actor=user.username)
    await session.commit()
    if not result["restored"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, result["reason"])
    detail = await board.load_order_detail(session, order_id)
    detail["books"] = [{"line_id": str(line_id), **result}]
    return detail


class FeesRequest(BaseModel):
    """Etsy's cut, typed by a person.

    Strings rather than floats: these are money and go straight into a
    QuickBooks document, where 0.1 + 0.2 must not be 0.30000000000000004.
    """

    etsy_fees: str | float | int | None = None
    marketing_fees: str | float | int | None = None
    processing_fees: str | float | int | None = None


def _fee_amount(raw: Any, field: str) -> Decimal | None:
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = Decimal(str(raw).strip().lstrip("$"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"{field} is not a number."
        ) from exc
    if value < 0:
        # Etsy's ledger states fees as money leaving; PrintFlow stores the size
        # of the bite. A minus sign here is somebody copying the ledger's sign.
        value = -value
    return value.quantize(Decimal("0.0001"))


@router.put("/orders/{order_id}/fees")
async def set_fees(
    order_id: uuid.UUID,
    body: FeesRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Type Etsy's fees in by hand.

    Etsy's ledger settles days after a sale, and until it does an order shows
    no cost at all — so a shop closing its month either waits or works it out
    on paper. This is the third option. What is typed is marked as typed, and
    *Check Etsy for fees* then leaves this order alone rather than replacing a
    figure somebody may already have expensed. Clearing every box hands the
    order back to the sweep.
    """
    order = await _get_order(session, order_id)
    values = {
        field: _fee_amount(getattr(body, field), field)
        for field in ("etsy_fees", "marketing_fees", "processing_fees")
    }
    for field, value in values.items():
        setattr(order, field, value)
    entered = any(value is not None for value in values.values())
    order.fees_source = "manual" if entered else None
    if not entered:
        # Emptied on purpose: drop the lines behind the old totals too, rather
        # than leaving a breakdown that no longer adds up to anything.
        order.fee_lines = None
    await audit.record(
        session,
        entity_type="order",
        entity_id=order.id,
        action="fees_entered" if entered else "fees_cleared",
        detail={
            "order_number": order.order_number,
            **{field: str(value) if value is not None else None
               for field, value in values.items()},
        },
        actor=user.username,
    )
    await session.commit()
    return await board.load_order_detail(session, order_id)


@router.post("/orders/{order_id}/expenses/{kind}")
async def create_expense(
    order_id: uuid.UUID,
    kind: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Expense what this order cost: the carrier's postage, or Etsy's cut.

    A button rather than something that happens on its own, for the same reason
    the invoice is one: it is a document in somebody's books.
    """
    if kind not in (books.SHIPPING_EXPENSE, books.FEE_EXPENSE):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No expense called '{kind}'.")
    order = await _get_order(session, order_id)
    try:
        await books.post_expense(session, order, kind, actor=user.username)
    except BooksError as exc:
        await session.commit()  # keep the recorded reason, not the write
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return await board.load_order_detail(session, order_id)


@router.post("/orders/{order_id}/expenses/{kind}/void")
async def remove_expense(
    order_id: uuid.UUID,
    kind: str,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Take one of an order's expenses back out of QuickBooks."""
    if kind not in (books.SHIPPING_EXPENSE, books.FEE_EXPENSE):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No expense called '{kind}'.")
    order = await _get_order(session, order_id)
    try:
        await books.void_expense(session, order, kind, actor=user.username)
    except BooksError as exc:
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return await board.load_order_detail(session, order_id)


@router.get("/books/invoice-number")
async def invoice_number(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """What the next invoice will be numbered, and who does the numbering.

    Read-only and asked before the button is pressed, because "what number will
    this get" is a question with two different answers depending on a setting
    inside the company file — and the wrong assumption is only discovered when
    an invoice turns up unnumbered.
    """
    return await books.invoice_numbering(session)


class CreateInvoiceRequest(BaseModel):
    # What day the invoice is dated. Omitted means the day the order was
    # placed, which is the honest default — the sale happened when it happened.
    # It exists because QuickBooks refuses to date anything touching an
    # inventory item before the day it started counting that item, which an
    # order placed before the items were set up trips on every line.
    invoice_date: date | None = None


@router.post("/orders/{order_id}/invoice")
async def create_invoice(
    order_id: uuid.UUID,
    body: CreateInvoiceRequest | None = None,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Raise this order's QuickBooks invoice.

    Deliberately a button rather than something that happens on its own: an
    invoice is a document in somebody's books, and which orders get one is a
    decision about the business rather than about the software.
    """
    order = await _get_order(session, order_id)
    try:
        await books.invoice_order(
            session,
            order,
            actor=user.username,
            invoice_date=(body.invoice_date if body else None),
        )
    except BooksError as exc:
        await session.commit()  # keep the recorded reason, not the write
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return await board.load_order_detail(session, order_id)


@router.post("/orders/{order_id}/invoice/void")
async def void_invoice(
    order_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Void this order's invoice, which frees it to be invoiced again."""
    order = await _get_order(session, order_id)
    try:
        await books.void_invoice(session, order, actor=user.username)
    except BooksError as exc:
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await session.commit()
    return await board.load_order_detail(session, order_id)


@router.post("/orders/{order_id}/invoice/clear")
async def clear_invoice(
    order_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Let go of the invoice link without calling QuickBooks.

    For an invoice that has already been deleted in QuickBooks, where a void
    can only fail — and where failing leaves the order claiming an invoice
    that does not exist and refusing to raise another.
    """
    order = await _get_order(session, order_id)
    try:
        await books.clear_invoice(session, order, actor=user.username)
    except BooksError as exc:
        await session.commit()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
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


@router.post("/orders/finances/refresh")
async def refresh_finances(
    _: User = Depends(require_user), session: AsyncSession = Depends(get_session)
) -> dict:
    """Read Etsy's ledger now rather than waiting for the next poll.

    Fees land after the sale, so a shop looking at an order that sold this
    morning will see none. This is the answer to "have they turned up yet".
    """
    # The address and totals first: they are in payloads already stored, so
    # they work even with Etsy disconnected.
    stats = await finance.backfill(session)
    try:
        stats |= await finance.sync_fees(session)
    except IntegrationNotConfigured as exc:
        await session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        await session.commit()
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    await session.commit()
    return stats


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


@router.get("/orders/{order_id}/label-rates")
async def label_rates(
    order_id: uuid.UUID,
    carrier_code: str,
    weight_value: float,
    weight_units: str = "ounces",
    package_code: str = "",
    confirmation: str = "",
    warehouse_id: str = "",
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What the carrier would charge, so the price is known before the click.

    Read-only: quoting costs nothing and buys nothing, which is the whole
    reason it can be called on every keystroke of the weight box.
    """
    order = await _get_order(session, order_id)
    try:
        return await shipping.label_rates(
            session,
            order,
            carrier_code=carrier_code,
            weight_value=weight_value,
            weight_units=weight_units,
            package_code=package_code,
            confirmation=confirmation,
            warehouse_id=warehouse_id or None,
        )
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        # A carrier that will not quote is not a reason to block the purchase:
        # the operator can still buy the label, just without knowing first.
        return {"available": False, "reason": str(exc)}


class CreateLabelRequest(BaseModel):
    carrier_code: str
    service_code: str
    package_code: str = "package"
    weight_value: float = Field(gt=0)
    weight_units: str = "ounces"
    confirmation: str | None = None
    # Which ship-from location this parcel leaves from. Omitted means "whatever
    # the shop default and the order resolve to" — the behaviour every label
    # before this had.
    warehouse_id: str | None = None
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
            warehouse_id=body.warehouse_id,
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
            # Which building it actually left from. A label bought from the
            # wrong origin is a real cost, and the audit trail should say.
            "ship_from_warehouse_id": result.get("ship_from_warehouse_id"),
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
