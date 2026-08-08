"""Products, single-level BOMs, and Bambuddy print mappings."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..auth import require_user
from ..db import get_session
from ..models import (
    FULFILLMENT_TYPES,
    BomLine,
    OrderLine,
    PrintMapping,
    Product,
    User,
)
from ..services import audit

router = APIRouter(prefix="/api/products", tags=["products"])


def _serialize(product: Product) -> dict[str, Any]:
    mapping = product.print_mapping
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "fulfillment": product.fulfillment,
        "qbo_item_id": product.qbo_item_id,
        "qbo_item_name": product.qbo_item_name,
        "active": product.active,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
        "print_mapping": (
            {
                "id": mapping.id,
                "bambuddy_archive_id": mapping.bambuddy_archive_id,
                "bambuddy_archive_name": mapping.bambuddy_archive_name,
                "plate_number": mapping.plate_number,
                "units_per_plate": mapping.units_per_plate,
                "print_options": mapping.print_options,
                "preferred_printer_id": mapping.preferred_printer_id,
            }
            if mapping
            else None
        ),
        "bom": [
            {
                "id": line.id,
                "component_id": line.component_id,
                "component_sku": line.component.sku if line.component else None,
                "component_name": line.component.name if line.component else None,
                "component_fulfillment": (
                    line.component.fulfillment if line.component else None
                ),
                "quantity": line.quantity,
            }
            for line in sorted(product.bom_lines, key=lambda line: line.created_at)
        ],
    }


async def _get(session: AsyncSession, product_id: uuid.UUID) -> Product:
    product = (
        await session.execute(
            select(Product)
            .where(Product.id == product_id)
            .options(
                selectinload(Product.print_mapping),
                selectinload(Product.bom_lines).selectinload(BomLine.component),
            )
            # Sessions do not expire on commit, so without this the identity map
            # would hand back the collections as they were before the write.
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if product is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
    return product


@router.get("")
async def list_products(
    q: str = "",
    fulfillment: str | None = None,
    include_inactive: bool = True,
    limit: int = 500,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    stmt = select(Product).options(
        selectinload(Product.print_mapping),
        selectinload(Product.bom_lines).selectinload(BomLine.component),
    )
    if q.strip():
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            func.lower(Product.sku).like(needle) | func.lower(Product.name).like(needle)
        )
    if fulfillment:
        stmt = stmt.where(Product.fulfillment == fulfillment)
    if not include_inactive:
        stmt = stmt.where(Product.active.is_(True))
    stmt = stmt.order_by(Product.sku).limit(max(1, min(limit, 2000)))
    products = (await session.execute(stmt)).scalars().all()
    return {"products": [_serialize(p) for p in products]}


class ProductRequest(BaseModel):
    sku: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=500)
    fulfillment: str
    qbo_item_id: str | None = None
    qbo_item_name: str | None = None
    active: bool = True


def _validate_fulfillment(value: str) -> str:
    if value not in FULFILLMENT_TYPES:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"fulfillment must be one of {', '.join(FULFILLMENT_TYPES)}",
        )
    return value


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    _validate_fulfillment(body.fulfillment)
    product = Product(
        sku=body.sku.strip(),
        name=body.name.strip(),
        fulfillment=body.fulfillment,
        qbo_item_id=body.qbo_item_id,
        qbo_item_name=body.qbo_item_name,
        active=body.active,
    )
    session.add(product)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A product with SKU '{body.sku.strip()}' already exists"
        ) from exc
    return _serialize(await _get(session, product.id))


@router.get("/{product_id}")
async def get_product(
    product_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    return _serialize(await _get(session, product_id))


@router.put("/{product_id}")
async def update_product(
    product_id: uuid.UUID,
    body: ProductRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    product = await _get(session, product_id)
    _validate_fulfillment(body.fulfillment)

    if body.fulfillment != product.fulfillment:
        if product.fulfillment == "bundle" and product.bom_lines:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Remove the BOM components before changing this product away from 'bundle'.",
            )
        if body.fulfillment == "bundle":
            used_as_component = (
                await session.execute(
                    select(BomLine.id).where(BomLine.component_id == product.id).limit(1)
                )
            ).first()
            if used_as_component:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "This product is a component of another bundle. BOMs are single-level, "
                    "so it cannot become a bundle itself.",
                )
        if body.fulfillment != "printed" and product.print_mapping is not None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Remove the Bambuddy print mapping before changing the fulfillment type.",
            )

    product.sku = body.sku.strip()
    product.name = body.name.strip()
    product.fulfillment = body.fulfillment
    product.qbo_item_id = body.qbo_item_id
    product.qbo_item_name = body.qbo_item_name
    product.active = body.active
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"A product with SKU '{body.sku.strip()}' already exists"
        ) from exc
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}")
async def delete_product(
    product_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    product = await _get(session, product_id)
    in_use = (
        await session.execute(
            select(OrderLine.id).where(OrderLine.product_id == product.id).limit(1)
        )
    ).first()
    if in_use:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This product is referenced by existing orders. Deactivate it instead of deleting.",
        )
    used_as_component = (
        await session.execute(
            select(BomLine.id).where(BomLine.component_id == product.id).limit(1)
        )
    ).first()
    if used_as_component:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This product is a component of a bundle. Remove it from that BOM first.",
        )
    await session.delete(product)
    await session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# BOM editor (single-level only, §3)
# --------------------------------------------------------------------------


class BomLineRequest(BaseModel):
    component_id: uuid.UUID
    quantity: int = Field(gt=0)


@router.post("/{product_id}/bom", status_code=status.HTTP_201_CREATED)
async def add_bom_line(
    product_id: uuid.UUID,
    body: BomLineRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    bundle = await _get(session, product_id)
    if bundle.fulfillment != "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only products with fulfillment 'bundle' have a BOM."
        )
    component = await session.get(Product, body.component_id)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Component product not found")
    if component.fulfillment == "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "BOMs are single-level: a bundle may not contain another bundle.",
        )
    if component.id == bundle.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A bundle cannot contain itself.")

    session.add(
        BomLine(bundle_id=bundle.id, component_id=component.id, quantity=body.quantity)
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That component is already on this bundle's BOM."
        ) from exc
    await audit.record(
        session,
        entity_type="product",
        entity_id=bundle.id,
        action="bom_add",
        detail={"component_sku": component.sku, "quantity": body.quantity},
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.put("/{product_id}/bom/{bom_line_id}")
async def update_bom_line(
    product_id: uuid.UUID,
    bom_line_id: uuid.UUID,
    body: BomLineRequest,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    line = await session.get(BomLine, bom_line_id)
    if line is None or line.bundle_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "BOM line not found")
    line.quantity = body.quantity
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}/bom/{bom_line_id}")
async def delete_bom_line(
    product_id: uuid.UUID,
    bom_line_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    line = await session.get(BomLine, bom_line_id)
    if line is None or line.bundle_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "BOM line not found")
    await session.delete(line)
    await session.commit()
    return _serialize(await _get(session, product_id))


# --------------------------------------------------------------------------
# Print mapping (attached via the Bambuddy archive browser, §4.3)
# --------------------------------------------------------------------------


class PrintMappingRequest(BaseModel):
    bambuddy_archive_id: int
    bambuddy_archive_name: str | None = None
    plate_number: int = Field(default=1, gt=0)
    units_per_plate: int = Field(default=1, gt=0)
    print_options: dict[str, Any] = Field(default_factory=dict)
    preferred_printer_id: int | None = None


@router.put("/{product_id}/print-mapping")
async def upsert_print_mapping(
    product_id: uuid.UUID,
    body: PrintMappingRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    product = await _get(session, product_id)
    if product.fulfillment != "printed":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only products with fulfillment 'printed' can have a Bambuddy mapping.",
        )
    mapping = product.print_mapping or PrintMapping(product_id=product.id)
    mapping.bambuddy_archive_id = body.bambuddy_archive_id
    mapping.bambuddy_archive_name = body.bambuddy_archive_name
    mapping.plate_number = body.plate_number
    mapping.units_per_plate = body.units_per_plate
    mapping.print_options = body.print_options
    mapping.preferred_printer_id = body.preferred_printer_id
    session.add(mapping)
    await session.flush()
    await audit.record(
        session,
        entity_type="product",
        entity_id=product.id,
        action="print_mapping_set",
        detail={
            "archive_id": body.bambuddy_archive_id,
            "plate_number": body.plate_number,
            "units_per_plate": body.units_per_plate,
        },
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}/print-mapping")
async def delete_print_mapping(
    product_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    product = await _get(session, product_id)
    if product.print_mapping is not None:
        await session.delete(product.print_mapping)
        await session.commit()
    return _serialize(await _get(session, product_id))
