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
    BomOptionRule,
    EtsyProductLink,
    OrderLine,
    PrintMapping,
    Product,
    User,
)
from ..services import audit, intake

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
        "option_rules": [
            {
                "id": rule.id,
                "option_name": rule.option_name,
                "option_value": rule.option_value,
                "replaces_id": rule.replaces_id,
                "replaces_sku": rule.replaces.sku if rule.replaces else None,
                "component_id": rule.component_id,
                "component_sku": rule.component.sku if rule.component else None,
                "quantity": rule.quantity,
            }
            for rule in sorted(product.option_rules, key=lambda rule: rule.created_at)
        ],
        # Etsy listings that resolve to this product without a SKU.
        "etsy_links": [
            {
                "id": link.id,
                "etsy_listing_id": link.etsy_listing_id,
                "etsy_product_id": link.etsy_product_id,
                "listing_title": link.listing_title,
            }
            for link in sorted(product.etsy_links, key=lambda link: link.created_at)
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
                selectinload(Product.option_rules).selectinload(BomOptionRule.component),
                selectinload(Product.option_rules).selectinload(BomOptionRule.replaces),
                selectinload(Product.etsy_links),
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
        selectinload(Product.option_rules).selectinload(BomOptionRule.component),
        selectinload(Product.option_rules).selectinload(BomOptionRule.replaces),
        selectinload(Product.etsy_links),
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


# --------------------------------------------------------------------------
# Etsy option rules
# --------------------------------------------------------------------------


class OptionRuleRequest(BaseModel):
    option_name: str = Field(min_length=1, max_length=200)
    option_value: str = Field(min_length=1, max_length=200)
    component_id: uuid.UUID
    # Null means "add this component"; set means "swap that one for this one".
    replaces_id: uuid.UUID | None = None
    # Null on a swap keeps the quantity from the BOM line being replaced.
    quantity: int | None = Field(default=None, gt=0)


@router.get("/{product_id}/observed-options")
async def observed_options(
    product_id: uuid.UUID,
    limit: int = 500,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The option names and values orders for this product have actually carried.

    Rules match on Etsy's own strings, which are typed by hand in the listing
    editor and are not visible anywhere in PrintFlow otherwise. Asking someone
    to reproduce them from memory is how you get a rule that silently never
    fires, so the choices are offered from what really arrived.
    """
    rows = (
        (
            await session.execute(
                select(OrderLine.variations)
                .where(OrderLine.product_id == product_id)
                .order_by(OrderLine.created_at.desc())
                .limit(max(1, min(limit, 2000)))
            )
        )
        .scalars()
        .all()
    )

    seen: dict[str, dict[str, Any]] = {}
    for variations in rows:
        for variation in variations or []:
            if not isinstance(variation, dict) or variation.get("free_text"):
                continue
            name, value = variation.get("name"), variation.get("value")
            if not name or not value:
                continue
            entry = seen.setdefault(str(name), {"name": str(name), "values": {}})
            entry["values"][str(value)] = entry["values"].get(str(value), 0) + 1

    return {
        "options": [
            {
                "name": entry["name"],
                "values": [
                    {"value": value, "orders": count}
                    for value, count in sorted(
                        entry["values"].items(), key=lambda pair: (-pair[1], pair[0])
                    )
                ],
            }
            for entry in sorted(seen.values(), key=lambda e: e["name"].lower())
        ]
    }


@router.post("/{product_id}/option-rules", status_code=status.HTTP_201_CREATED)
async def add_option_rule(
    product_id: uuid.UUID,
    body: OptionRuleRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    bundle = await _get(session, product_id)
    if bundle.fulfillment != "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only a bundle has a BOM for an option to change. Model the option-driven "
            "part as a bundle component first.",
        )

    component = await session.get(Product, body.component_id)
    if component is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Component product not found")
    if component.fulfillment == "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "BOMs are single-level: an option cannot bring in another bundle.",
        )
    if component.id == bundle.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A bundle cannot contain itself.")

    if body.replaces_id is not None:
        if body.replaces_id == body.component_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "A rule cannot swap a component for itself."
            )
        on_bom = (
            await session.execute(
                select(BomLine.id).where(
                    BomLine.bundle_id == bundle.id, BomLine.component_id == body.replaces_id
                )
            )
        ).first()
        if not on_bom:
            # A rule pointing at a component the bundle does not have would be
            # skipped at intake and the order built wrong, so refuse it here
            # while someone is looking at the screen.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "The component being replaced is not on this bundle's BOM.",
            )

    session.add(
        BomOptionRule(
            bundle_id=bundle.id,
            option_name=body.option_name.strip(),
            option_value=body.option_value.strip(),
            replaces_id=body.replaces_id,
            component_id=body.component_id,
            quantity=body.quantity,
        )
    )
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"A rule for {body.option_name.strip()} = {body.option_value.strip()} "
            "already brings in that component.",
        ) from exc
    await audit.record(
        session,
        entity_type="product",
        entity_id=bundle.id,
        action="option_rule_add",
        detail={
            "option": f"{body.option_name.strip()} = {body.option_value.strip()}",
            "component_sku": component.sku,
        },
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}/option-rules/{rule_id}")
async def delete_option_rule(
    product_id: uuid.UUID,
    rule_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    rule = await session.get(BomOptionRule, rule_id)
    if rule is None or rule.bundle_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Rule not found")
    await session.delete(rule)
    await session.commit()
    return _serialize(await _get(session, product_id))


# --------------------------------------------------------------------------
# Etsy listing links — for listings that carry no SKU
# --------------------------------------------------------------------------


class EtsyLinkRequest(BaseModel):
    etsy_listing_id: int = Field(gt=0)
    # Leave unset to cover the whole listing, which is usually what you want:
    # Etsy regenerates a variant's product id whenever the seller edits the
    # listing's options, and per-variant differences belong in option rules.
    etsy_product_id: int | None = None
    listing_title: str | None = None


@router.post("/{product_id}/etsy-links", status_code=status.HTTP_201_CREATED)
async def add_etsy_link(
    product_id: uuid.UUID,
    body: EtsyLinkRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    product = await _get(session, product_id)
    link = EtsyProductLink(
        product_id=product.id,
        etsy_listing_id=body.etsy_listing_id,
        etsy_product_id=body.etsy_product_id,
        listing_title=(body.listing_title or "").strip() or None,
    )
    session.add(link)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Etsy listing {body.etsy_listing_id} is already linked to a product. "
            "Remove that link first.",
        ) from exc

    # Orders already sitting unmatched on this listing are the reason someone
    # adds a link by hand, so clear them here rather than making them go find
    # each one.
    fixed = await intake.apply_etsy_link(session, link)

    await audit.record(
        session,
        entity_type="product",
        entity_id=product.id,
        action="etsy_link_add",
        detail={
            "etsy_listing_id": body.etsy_listing_id,
            "etsy_product_id": body.etsy_product_id,
            "also_fixed": fixed,
        },
        actor=user.username,
    )
    await session.commit()
    result = _serialize(await _get(session, product_id))
    result["also_fixed"] = fixed
    return result


@router.delete("/{product_id}/etsy-links/{link_id}")
async def delete_etsy_link(
    product_id: uuid.UUID,
    link_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    link = await session.get(EtsyProductLink, link_id)
    if link is None or link.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    await session.delete(link)
    await session.commit()
    return _serialize(await _get(session, product_id))
