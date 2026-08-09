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
    LINE_CANCELLED,
    LINE_SHIPPED,
    MadeSheetLine,
    OrderLine,
    PrintMapping,
    Product,
    ProductVariation,
    User,
)
from ..services import audit, codes, intake, printing
from ..services import variations as variations_service
from ..services.credentials import IntegrationNotConfigured

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
        "variations": [
            {
                "id": variation.id,
                "label": variation.label,
                "options": variation.options,
                "etsy_listing_id": variation.etsy_listing_id,
                "etsy_product_id": variation.etsy_product_id,
                "bambuddy_archive_id": variation.bambuddy_archive_id,
                "bambuddy_archive_name": variation.bambuddy_archive_name,
                "plate_number": variation.plate_number,
                "units_per_plate": variation.units_per_plate,
                "preferred_printer_id": variation.preferred_printer_id,
                "qbo_item_id": variation.qbo_item_id,
                "qbo_item_name": variation.qbo_item_name,
                "active": variation.active,
            }
            for variation in sorted(
                product.variations, key=lambda variation: variation.label.lower()
            )
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
                selectinload(Product.variations),
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
        selectinload(Product.variations),
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
    # Optional: Etsy never required a SKU, so neither does PrintFlow. Left
    # blank, a code is generated from the Etsy listing this product is linked
    # to, or from its name.
    sku: str | None = Field(default=None, max_length=200)
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
    code = (body.sku or "").strip() or await codes.unique(
        session, codes.from_name(body.name)
    )
    product = Product(
        sku=code,
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
            status.HTTP_409_CONFLICT, f"A product with code '{code}' already exists"
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

    # Clearing the code regenerates one rather than leaving the product with
    # nothing to print next to it.
    code = (body.sku or "").strip() or product.sku
    product.sku = code
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
            status.HTTP_409_CONFLICT, f"A product with code '{code}' already exists"
        ) from exc
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}")
async def delete_product(
    product_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    product = await _get(session, product_id)
    blockers = await _delete_blockers(session, product)
    if blockers:
        raise HTTPException(status.HTTP_409_CONFLICT, " ".join(blockers))
    await session.delete(product)
    await session.commit()
    return {"ok": True}


class BulkDeleteRequest(BaseModel):
    product_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


@router.post("/bulk-delete")
async def bulk_delete_products(
    body: BulkDeleteRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete several products at once, skipping the ones that cannot go.

    A bulk import can make a hundred products in a click, so undoing one has to
    be equally cheap. Partial success on purpose: one product held back by an
    order it appeared on should not strand the other ninety-nine.
    """
    deleted: list[str] = []
    kept: list[dict[str, Any]] = []
    for product_id in body.product_ids:
        product = await session.get(Product, product_id)
        if product is None:
            continue
        blockers = await _delete_blockers(session, product)
        if blockers:
            kept.append(
                {"id": str(product.id), "name": product.name, "reason": " ".join(blockers)}
            )
            continue
        deleted.append(product.name)
        await session.delete(product)
        await session.flush()

    await audit.record(
        session,
        entity_type="product",
        entity_id=None,
        action="bulk_delete",
        detail={"deleted": len(deleted), "kept": len(kept)},
        actor=user.username,
    )
    await session.commit()
    return {"deleted": len(deleted), "kept": kept}


async def _delete_blockers(session: AsyncSession, product: Product) -> list[str]:
    """Everything standing in the way of deleting this product.

    All of them, not the first one: finding out about the next obstacle only
    after clearing the last is a miserable way to tidy up a catalogue. Every
    table with a restricting foreign key to products has to be represented
    here, or the delete fails in the database and surfaces as a 500.
    """
    blockers: list[str] = []

    orders = (
        await session.execute(
            select(func.count())
            .select_from(OrderLine)
            .where(OrderLine.product_id == product.id)
        )
    ).scalar_one()
    if orders:
        blockers.append(
            f"{orders} order line{'s' if orders != 1 else ''} "
            f"reference{'' if orders != 1 else 's'} it — mark it inactive instead, "
            "so the order history stays readable."
        )

    components = (
        await session.execute(
            select(func.count())
            .select_from(BomLine)
            .where(BomLine.component_id == product.id)
        )
    ).scalar_one()
    if components:
        blockers.append(
            f"It is a component of {components} bundle{'s' if components != 1 else ''} — "
            "remove it from those BOMs first."
        )

    rules = (
        await session.execute(
            select(func.count())
            .select_from(BomOptionRule)
            .where(BomOptionRule.component_id == product.id)
        )
    ).scalar_one()
    if rules:
        blockers.append(
            f"{rules} option rule{'s' if rules != 1 else ''} "
            f"bring{'' if rules != 1 else 's'} it in — delete those rules first."
        )

    sheets = (
        await session.execute(
            select(func.count())
            .select_from(MadeSheetLine)
            .where(MadeSheetLine.product_id == product.id)
        )
    ).scalar_one()
    if sheets:
        blockers.append(
            f"{sheets} made-items line{'s' if sheets != 1 else ''} record{'' if sheets != 1 else 's'} "
            "making it — those are accounting history and are never rewritten, so "
            "mark it inactive instead."
        )

    return blockers


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


# --------------------------------------------------------------------------
# Variations — the buyable combinations of a product's options
# --------------------------------------------------------------------------


@router.post("/{product_id}/variations/sync-etsy")
async def sync_variations_from_etsy(
    product_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Read the linked listings and make a variation for every combination.

    Etsy already describes exactly which combinations it sells, so nobody
    should be typing option names by hand and hoping they match — a typo there
    is silent, and silently prints the wrong plate.

    Re-running is how a listing edit gets picked up: combinations already known
    keep their overrides and have their Etsy id refreshed, new ones are added,
    and ones Etsy no longer offers are deactivated rather than deleted, because
    old orders still point at them.
    """
    product = await _get(session, product_id)
    listing_ids = [link.etsy_listing_id for link in product.etsy_links]
    if not listing_ids:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Link this product to an Etsy listing first — its variations come from there.",
        )

    from ..integrations import etsy as etsy_api
    from ..integrations.base import IntegrationError
    from ..routers.integrations_router import _etsy_client

    try:
        client = await _etsy_client(session)
        found: list[dict[str, Any]] = []
        for listing_id in listing_ids:
            inventory = await client.listing_inventory(listing_id)
            variants = etsy_api.listing_variants({"listing_id": listing_id}, inventory)
            found.extend(variations_service.from_etsy_variants(variants, listing_id))
    except IntegrationNotConfigured as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except IntegrationError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    result = await _apply_variations(session, product, found)
    await audit.record(
        session,
        entity_type="product",
        entity_id=product.id,
        action="variations_sync_etsy",
        detail=result,
        actor=user.username,
    )
    await session.commit()
    payload = _serialize(await _get(session, product_id))
    payload.update(result)
    return payload


async def _apply_variations(
    session: AsyncSession, product: Product, found: list[dict[str, Any]]
) -> dict[str, int]:
    """Merge freshly-read combinations into the product's variations."""
    existing = {
        variations_service.option_key(variation.options): variation
        for variation in product.variations
    }
    added = updated = 0
    seen: set = set()

    for row in found:
        key = variations_service.option_key(row["options"])
        seen.add(key)
        variation = existing.get(key)
        if variation is None:
            session.add(
                ProductVariation(
                    product_id=product.id,
                    options=row["options"],
                    label=row["label"],
                    etsy_listing_id=row["etsy_listing_id"],
                    etsy_product_id=row["etsy_product_id"],
                )
            )
            added += 1
            continue
        # Keep every override; only the identity Etsy owns is refreshed.
        variation.etsy_listing_id = row["etsy_listing_id"]
        variation.etsy_product_id = row["etsy_product_id"]
        variation.label = row["label"]
        if not variation.active:
            variation.active = True
        updated += 1

    retired = 0
    for key, variation in existing.items():
        if key not in seen and variation.active:
            variation.active = False
            retired += 1

    await session.flush()

    # Orders already on the board were matched before these existed, so they
    # carry no variation and would print the product's default plate. Re-attach
    # them here: setting variations up after the first order arrives is the
    # normal way round, not an edge case.
    reattached = 0
    open_lines = (
        (
            await session.execute(
                select(OrderLine).where(
                    OrderLine.product_id == product.id,
                    OrderLine.state.notin_((LINE_SHIPPED, LINE_CANCELLED)),
                )
            )
        )
        .scalars()
        .all()
    )
    moved: list[OrderLine] = []
    for line in open_lines:
        was = line.variation_id
        await intake.attach_variation(session, line, product)
        if line.variation_id != was:
            reattached += 1
            moved.append(line)
    if moved:
        await session.flush()
        # Re-plan, so a line that just gained a variation stops pointing at the
        # product's default plate. Jobs already on the Bambuddy queue are left
        # alone by plan_jobs; only undispatched ones are corrected.
        await printing.plan_jobs(session, moved)

    return {
        "added": added,
        "updated": updated,
        "retired": retired,
        "reattached": reattached,
    }


class VariationRequest(BaseModel):
    """Every override is optional; null means "use the product's"."""

    bambuddy_archive_id: int | None = None
    bambuddy_archive_name: str | None = None
    plate_number: int | None = Field(default=None, gt=0)
    units_per_plate: int | None = Field(default=None, gt=0)
    preferred_printer_id: int | None = None
    qbo_item_id: str | None = None
    qbo_item_name: str | None = None
    active: bool = True


@router.put("/{product_id}/variations/{variation_id}")
async def update_variation(
    product_id: uuid.UUID,
    variation_id: uuid.UUID,
    body: VariationRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    variation = await session.get(ProductVariation, variation_id)
    if variation is None or variation.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variation not found")

    variation.bambuddy_archive_id = body.bambuddy_archive_id
    variation.bambuddy_archive_name = body.bambuddy_archive_name
    variation.plate_number = body.plate_number
    variation.units_per_plate = body.units_per_plate
    variation.preferred_printer_id = body.preferred_printer_id
    variation.qbo_item_id = body.qbo_item_id
    variation.qbo_item_name = body.qbo_item_name
    variation.active = body.active
    await session.flush()

    await audit.record(
        session,
        entity_type="product",
        entity_id=product_id,
        action="variation_update",
        detail={"variation": variation.label, "archive": body.bambuddy_archive_id},
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}/variations/{variation_id}")
async def delete_variation(
    product_id: uuid.UUID,
    variation_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    variation = await session.get(ProductVariation, variation_id)
    if variation is None or variation.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variation not found")
    # Orders that bought it keep their own record of what was chosen; the
    # foreign key clears itself rather than blocking a catalogue edit.
    await session.delete(variation)
    await session.commit()
    return _serialize(await _get(session, product_id))
