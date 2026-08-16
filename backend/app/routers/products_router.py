"""Products, single-level BOMs, and Bambuddy print mappings."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
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
    Order,
    OrderLine,
    PrintFile,
    Product,
    ProductOptionItem,
    ProductVariation,
    User,
)
from ..services import allocation, audit, codes, intake, printing
from ..services.state import recompute_order
from ..services import variations as variations_service
from ..services.credentials import IntegrationNotConfigured

router = APIRouter(prefix="/api/products", tags=["products"])


def _option_words(options: list[dict[str, Any]] | None) -> str:
    """A set of chosen options as a person would say it."""
    return " and ".join(
        f"{option.get('name')}: {option.get('value')}" for option in options or []
    )


def _serialize(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "sku": product.sku,
        "name": product.name,
        "fulfillment": product.fulfillment,
        "parent_id": product.parent_id,
        "qbo_item_id": product.qbo_item_id,
        "qbo_item_name": product.qbo_item_name,
        "active": product.active,
        "created_at": product.created_at,
        "updated_at": product.updated_at,
        "print_files": [
            {
                "id": row.id,
                "bambuddy_archive_id": row.bambuddy_archive_id,
                "bambuddy_archive_name": row.bambuddy_archive_name,
                "bambuddy_file_path": row.bambuddy_file_path,
                "bambuddy_printer_id": row.bambuddy_printer_id,
                "plate_number": row.plate_number,
                "units_per_plate": row.units_per_plate,
                "print_options": row.print_options,
                "printer_models": row.printer_models or [],
            }
            for row in product.print_files
        ],
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
                "bambuddy_file_path": variation.bambuddy_file_path,
                "bambuddy_printer_id": variation.bambuddy_printer_id,
                "plate_number": variation.plate_number,
                "units_per_plate": variation.units_per_plate,
                "printer_models": variation.printer_models or [],
                "qbo_item_id": variation.qbo_item_id,
                "qbo_item_name": variation.qbo_item_name,
                "variant_product_id": variation.variant_product_id,
                "variant_product_name": (
                    variation.variant_product.name if variation.variant_product else None
                ),
                "variant_product_fulfillment": (
                    variation.variant_product.fulfillment
                    if variation.variant_product
                    else None
                ),
                # What that product would be billed and drawn down against, so
                # the screen can say what a variation with no item of its own
                # actually falls back to rather than leaving it to be guessed.
                "variant_product_qbo_item_id": (
                    variation.variant_product.qbo_item_id
                    if variation.variant_product
                    else None
                ),
                "variant_product_qbo_item_name": (
                    variation.variant_product.qbo_item_name
                    if variation.variant_product
                    else None
                ),
                "active": variation.active,
            }
            for variation in sorted(
                product.variations, key=lambda variation: variation.label.lower()
            )
        ],
        # Which QuickBooks item each option is sold as. A listing sold in two
        # scales is two items on the books, so a product carries as many of
        # these as it has options worth telling apart.
        "option_items": [
            {
                "id": row.id,
                "options": row.options or [],
                "qbo_item_id": row.qbo_item_id,
                "qbo_item_name": row.qbo_item_name,
                "position": row.position,
            }
            for row in sorted(product.option_items, key=lambda row: row.position)
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
                selectinload(Product.print_files),
                selectinload(Product.bom_lines).selectinload(BomLine.component),
                selectinload(Product.option_rules).selectinload(BomOptionRule.component),
                selectinload(Product.option_rules).selectinload(BomOptionRule.replaces),
                selectinload(Product.etsy_links),
                selectinload(Product.option_items),
                selectinload(Product.variations).selectinload(ProductVariation.variant_product),
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
        selectinload(Product.print_files),
        selectinload(Product.bom_lines).selectinload(BomLine.component),
        selectinload(Product.option_rules).selectinload(BomOptionRule.component),
        selectinload(Product.option_rules).selectinload(BomOptionRule.replaces),
        selectinload(Product.etsy_links),
        selectinload(Product.option_items),
        selectinload(Product.variations).selectinload(ProductVariation.variant_product),
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


def _clean_models(models: list[str]) -> list[str]:
    """Trimmed, de-duplicated, order preserved. Empty means any printer."""
    seen: dict[str, str] = {}
    for model in models:
        name = str(model).strip()
        if name and name.casefold() not in seen:
            seen[name.casefold()] = name
    return list(seen.values())


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
        if body.fulfillment != "printed" and product.print_files:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Remove the Bambuddy print files before changing the fulfillment type.",
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

    # Variants before masters, so selecting a whole family and deleting it works
    # in one pass rather than reporting "it has 2 variants" about a product the
    # operator has already ticked.
    chosen = [
        product
        for product in (
            await session.execute(select(Product).where(Product.id.in_(body.product_ids)))
        )
        .scalars()
        .all()
    ]
    order_of = {product.id: (0 if product.parent_id else 1) for product in chosen}
    for product_id in sorted(body.product_ids, key=lambda pid: order_of.get(pid, 1)):
        product = await session.get(Product, product_id)
        if product is None:
            continue
        # A variation pointing at it goes too; the row without its product would
        # silently fall back to the master's build.
        for variation in (
            (
                await session.execute(
                    select(ProductVariation).where(
                        ProductVariation.variant_product_id == product.id
                    )
                )
            )
            .scalars()
            .all()
        ):
            variation.variant_product_id = None
        await session.flush()
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

    variants = (
        await session.execute(
            select(func.count()).select_from(Product).where(Product.parent_id == product.id)
        )
    ).scalar_one()
    if variants:
        blockers.append(
            f"It has {variants} variant{'s' if variants != 1 else ''} — delete "
            "those first, or select them here too."
        )

    used_by_variation = (
        await session.execute(
            select(func.count())
            .select_from(ProductVariation)
            .where(ProductVariation.variant_product_id == product.id)
        )
    ).scalar_one()
    if used_by_variation:
        blockers.append(
            f"{used_by_variation} variation{'s' if used_by_variation != 1 else ''} "
            "resolve to it — detach it from those first."
        )

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


async def _component_for_qbo_item(
    session: AsyncSession, item_id: str, item_name: str | None
) -> tuple[Product, bool]:
    """The product standing for a QuickBooks inventory item, made if need be.

    Components are products all the way down, because that is what the rest of
    the pipeline works in: allocation reads a product's QuickBooks item,
    printing reads its mapping, a made-items sheet rolls up its BOM. What this
    saves is the step in the middle — retyping a material as a product and then
    linking it back to the item you picked it from.

    An item that already has a product reuses it. Two products pointing at one
    item would be two things competing for the same stock.
    """
    item_id = item_id.strip()
    name = (item_name or "").strip() or f"QuickBooks item {item_id}"

    existing = (
        await session.execute(select(Product).where(Product.qbo_item_id == item_id))
    ).scalars().first()
    if existing is not None:
        return existing, False

    component = Product(
        sku=await codes.unique(session, codes.from_name(name)),
        name=name[:500],
        fulfillment="stocked",
        qbo_item_id=item_id,
        qbo_item_name=name[:500],
        active=True,
    )
    session.add(component)
    await session.flush()
    return component, True


class BomFromQboRequest(BaseModel):
    qbo_item_id: str = Field(min_length=1)
    qbo_item_name: str | None = None
    quantity: int = Field(gt=0, default=1)


@router.post("/{product_id}/bom/from-qbo", status_code=status.HTTP_201_CREATED)
async def add_bom_line_from_qbo(
    product_id: uuid.UUID,
    body: BomFromQboRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Put a QuickBooks inventory item on a bundle's BOM.

    The raw materials a bundle consumes — filament, magnets, a printed insert
    somebody else makes — are already in QuickBooks, and that is the copy the
    stock check reads. Making the operator retype each one as a product first,
    then link it back to the item they picked it from, is a step that can only
    be got wrong.

    A component is still a product underneath, because that is what the whole
    pipeline downstream works in. If one already points at this item it is
    reused; otherwise a stocked product is created for it. Either way the BOM
    ends up naming something whose stock QuickBooks can answer for.
    """
    bundle = await _get(session, product_id)
    if bundle.fulfillment != "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Only products with fulfillment 'bundle' have a BOM."
        )

    item_id = body.qbo_item_id.strip()
    component, created = await _component_for_qbo_item(
        session, item_id, body.qbo_item_name
    )
    if component.id == bundle.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A bundle cannot contain itself.")
    if component.fulfillment == "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "BOMs are single-level, and that QuickBooks item is already a bundle here.",
        )

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
        action="bom_add_from_qbo",
        detail={
            "qbo_item_id": item_id,
            "component_sku": component.sku,
            "quantity": body.quantity,
            "created_product": created,
        },
        actor=user.username,
    )
    await session.commit()
    payload = _serialize(await _get(session, product_id))
    payload["created_product"] = created
    return payload


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
# Print files (attached from the Bambuddy file picker)
# --------------------------------------------------------------------------


class PrintFileRequest(BaseModel):
    bambuddy_archive_id: int | None = None
    bambuddy_archive_name: str | None = None
    bambuddy_file_path: str | None = None
    bambuddy_printer_id: int | None = None
    plate_number: int = Field(default=1, gt=0)
    units_per_plate: int = Field(default=1, gt=0)
    print_options: dict[str, Any] = Field(default_factory=dict)
    printer_models: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _names_a_file(self) -> "PrintFileRequest":
        if self.bambuddy_archive_id is None and not (self.bambuddy_file_path or "").strip():
            raise ValueError(
                "A print file needs a file: either a Bambuddy archive id or a "
                "path from the file manager."
            )
        return self


def _apply_file(row: PrintFile, body: PrintFileRequest) -> None:
    row.bambuddy_archive_id = body.bambuddy_archive_id
    row.bambuddy_archive_name = body.bambuddy_archive_name
    row.bambuddy_file_path = (body.bambuddy_file_path or "").strip() or None
    row.bambuddy_printer_id = body.bambuddy_printer_id
    row.plate_number = body.plate_number
    row.units_per_plate = body.units_per_plate
    row.print_options = body.print_options
    # A file that lives on one machine is printed on that machine, so the models
    # have nothing left to decide and keeping them would only read as a second,
    # contradictory answer to the same question.
    row.printer_models = (
        [] if body.bambuddy_printer_id is not None else _clean_models(body.printer_models)
    )


async def _printed_product(session: AsyncSession, product_id: uuid.UUID) -> Product:
    product = await _get(session, product_id)
    if product.fulfillment != "printed":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only products with fulfillment 'printed' can have Bambuddy files.",
        )
    return product


@router.post("/{product_id}/print-files")
async def add_print_file(
    product_id: uuid.UUID,
    body: PrintFileRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Attach another way of printing this product.

    A product has one of these per way it can be made — the same part sliced for
    each machine that can take it — and which one gets used is decided when a
    plate is actually sent.
    """
    product = await _printed_product(session, product_id)
    row = PrintFile(product_id=product.id)
    _apply_file(row, body)
    session.add(row)
    await session.flush()
    await audit.record(
        session,
        entity_type="product",
        entity_id=product.id,
        action="print_file_added",
        detail={
            "archive_id": body.bambuddy_archive_id,
            "file_path": row.bambuddy_file_path,
            "printer_id": body.bambuddy_printer_id,
            "printer_models": row.printer_models,
        },
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.put("/{product_id}/print-files/{file_id}")
async def update_print_file(
    product_id: uuid.UUID,
    file_id: uuid.UUID,
    body: PrintFileRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    await _printed_product(session, product_id)
    row = await session.get(PrintFile, file_id)
    if row is None or row.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Print file not found")
    _apply_file(row, body)
    await session.flush()
    await audit.record(
        session,
        entity_type="product",
        entity_id=product_id,
        action="print_file_set",
        detail={
            "archive_id": body.bambuddy_archive_id,
            "file_path": row.bambuddy_file_path,
            "printer_id": body.bambuddy_printer_id,
            "printer_models": row.printer_models,
        },
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}/print-files/{file_id}")
async def delete_print_file(
    product_id: uuid.UUID,
    file_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(PrintFile, file_id)
    if row is not None and row.product_id == product_id:
        await session.delete(row)
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


class OptionRuleFromQboRequest(BaseModel):
    """An option rule whose component comes straight out of QuickBooks."""

    option_name: str = Field(min_length=1, max_length=200)
    option_value: str = Field(min_length=1, max_length=200)
    replaces_id: uuid.UUID | None = None
    quantity: int | None = Field(default=None, gt=0)
    qbo_item_id: str = Field(min_length=1)
    qbo_item_name: str | None = None


@router.post("/{product_id}/option-rules/from-qbo", status_code=status.HTTP_201_CREATED)
async def add_option_rule_from_qbo(
    product_id: uuid.UUID,
    body: OptionRuleFromQboRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Bring a QuickBooks item in for one variation of a bundle.

    A variation exists precisely because it needs something the base build does
    not: the fan, the bigger magnet, the second colour. So the thing it needs is
    by definition *not* on the BOM, and offering only what is already there — or
    only what is already a product — is offering the wrong list.
    """
    bundle = await _get(session, product_id)
    if bundle.fulfillment != "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Only a bundle has a BOM for an option to change. Model the option-driven "
            "part as a bundle component first.",
        )

    component, created = await _component_for_qbo_item(
        session, body.qbo_item_id, body.qbo_item_name
    )
    if component.id == bundle.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "A bundle cannot contain itself.")
    if component.fulfillment == "bundle":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "BOMs are single-level: an option cannot bring in another bundle.",
        )

    if body.replaces_id is not None:
        if body.replaces_id == component.id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "A rule cannot swap a component for itself."
            )
        on_bom = (
            await session.execute(
                select(BomLine.id).where(
                    BomLine.bundle_id == bundle.id,
                    BomLine.component_id == body.replaces_id,
                )
            )
        ).first()
        if not on_bom:
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
            component_id=component.id,
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
        action="option_rule_add_from_qbo",
        detail={
            "option": f"{body.option_name.strip()} = {body.option_value.strip()}",
            "qbo_item_id": body.qbo_item_id.strip(),
            "component_sku": component.sku,
            "created_product": created,
        },
        actor=user.username,
    )
    await session.commit()
    payload = _serialize(await _get(session, product_id))
    payload["created_product"] = created
    return payload


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
    # Same reason as the order payloads: never merge loose keys into a shape the
    # UI reads by name.
    payload["sync"] = result
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
        # Only ones Etsy told us about. "Etsy no longer offers this" is not a
        # statement anybody can make about a combination Etsy never offered:
        # a variation typed in by hand — for a listing whose options Etsy does
        # not model as inventory — would otherwise be switched off by the next
        # sync, silently, along with the QuickBooks item somebody put on it.
        if variation.etsy_product_id is None:
            continue
        if key not in seen and variation.active:
            variation.active = False
            retired += 1

    await session.flush()
    reattached = await _reattach_open_lines(session, product)

    return {
        "added": added,
        "updated": updated,
        "retired": retired,
        "reattached": reattached,
    }


async def _reattach_open_lines(session: AsyncSession, product: Product) -> int:
    """Point this product's open lines at whichever variation now describes them.

    Orders already on the board were matched before these variations existed,
    so they carry none and would print the product's default plate. Setting
    variations up after the first order arrives is the normal way round, not an
    edge case — so both the Etsy sync and a hand-made variation run this.
    """
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
    return reattached


class OptionPair(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=500)


class OptionItemRequest(BaseModel):
    # Several at once: a shop's items are not always split along one option.
    # Every one named has to be among the buyer's choices for it to match.
    options: list[OptionPair] = Field(min_length=1, max_length=20)
    qbo_item_id: str = Field(min_length=1, max_length=100)
    qbo_item_name: str | None = None


@router.post("/{product_id}/option-items", status_code=201)
async def add_option_item(
    product_id: uuid.UUID,
    body: OptionItemRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Say which QuickBooks item a chosen option is sold as.

    One listing is often several things on the books — a playset in HO and in
    1:64 — and the option the buyer picked is what says which. Set by hand, and
    only by hand: which of a shop's items a combination is sold as is a decision
    about their books, and deriving it would put a real sale on the wrong item.
    """
    product = await _get(session, product_id)
    options = [
        {"name": pair.name.strip(), "value": pair.value.strip()} for pair in body.options
    ]
    wanted = variations_service.option_key(options)
    if not wanted:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Give the mapping at least one option name and value — that is what "
            "an order is matched on.",
        )

    for existing in product.option_items:
        if variations_service.option_key(existing.options or []) == wanted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"“{_option_words(existing.options)}” is already sold as "
                f"{existing.qbo_item_name or existing.qbo_item_id}.",
            )

    row = ProductOptionItem(
        product_id=product.id,
        options=options,
        qbo_item_id=body.qbo_item_id.strip(),
        qbo_item_name=(body.qbo_item_name or "").strip() or None,
        # Last, so adding one never changes what the ones above it already do.
        # Being last does not make it lose: a mapping pinning more options wins
        # on specificity, and position only orders the equally specific.
        position=max((item.position for item in product.option_items), default=-1) + 1,
    )
    session.add(row)
    await session.flush()

    await audit.record(
        session,
        entity_type="product",
        entity_id=product.id,
        action="option_item_added",
        detail={
            "options": _option_words(options),
            "qbo_item": row.qbo_item_name or row.qbo_item_id,
        },
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.post("/{product_id}/option-items/{item_id}/move")
async def move_option_item(
    product_id: uuid.UUID,
    item_id: uuid.UUID,
    up: bool = True,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Reorder the mappings, which is what decides ties.

    A buyer can pick a scale *and* a loadout, and both can name an item. Which
    one the sale is booked as is the operator's call, not something to infer —
    so it is the order on the screen, and the order is theirs to set.
    """
    product = await _get(session, product_id)
    rows = sorted(product.option_items, key=lambda row: row.position)
    index = next((i for i, row in enumerate(rows) if row.id == item_id), None)
    if index is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")

    swap = index - 1 if up else index + 1
    if 0 <= swap < len(rows):
        rows[index], rows[swap] = rows[swap], rows[index]
    # Renumber the lot: positions may have gaps from earlier deletes, and a
    # swap of two numbers that were never contiguous does nothing visible.
    for position, row in enumerate(rows):
        row.position = position
    await session.flush()
    await session.commit()
    return _serialize(await _get(session, product_id))


@router.delete("/{product_id}/option-items/{item_id}")
async def delete_option_item(
    product_id: uuid.UUID,
    item_id: uuid.UUID,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    row = await session.get(ProductOptionItem, item_id)
    if row is None or row.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mapping not found")
    await session.delete(row)
    await audit.record(
        session,
        entity_type="product",
        entity_id=product_id,
        action="option_item_removed",
        detail={"options": _option_words(row.options)},
        actor=user.username,
    )
    await session.commit()
    return _serialize(await _get(session, product_id))


class NewVariationRequest(BaseModel):
    label: str | None = Field(default=None, max_length=300)
    # At least one: a variation that pins nothing can never match an order.
    # `automatch` requires a non-empty option set before it will consider one,
    # precisely so that an empty variation does not swallow every line.
    options: list[OptionPair] = Field(min_length=1, max_length=20)
    qbo_item_id: str | None = None
    qbo_item_name: str | None = None


@router.post("/{product_id}/variations", status_code=201)
async def create_variation(
    product_id: uuid.UUID,
    body: NewVariationRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Add a variation by hand, for what Etsy will not hand over.

    Pulling from Etsy is the right way round and stays the default: Etsy states
    exactly which combinations it sells, and typing option names by hand risks
    a typo that silently prints the wrong plate. But it only works for listings
    whose options Etsy models as inventory. A listing that describes its scales
    in the title, or offers them as a made-to-order choice, has no combinations
    to read — and until now that meant the product had no variations at all,
    and nowhere to put a QuickBooks item.

    Matching still works the same way: an order matches on the option values
    when there is no Etsy id to match on, so a hand-made variation whose names
    and values match Etsy's wording picks up orders exactly as a pulled one
    does. Which is why the screen says to copy that wording exactly.
    """
    product = await _get(session, product_id)
    options = [{"name": pair.name.strip(), "value": pair.value.strip()} for pair in body.options]
    key = variations_service.option_key(options)
    if not key:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Give the variation at least one option name and value — that is "
            "what an order is matched on.",
        )

    for existing in product.variations:
        if variations_service.option_key(existing.options) == key:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"“{existing.label}” already covers that combination.",
            )

    variation = ProductVariation(
        product_id=product.id,
        options=options,
        label=(body.label or "").strip() or variations_service.label_for(options),
        qbo_item_id=body.qbo_item_id,
        qbo_item_name=body.qbo_item_name,
    )
    session.add(variation)
    await session.flush()

    # Same reason the Etsy sync does it: orders already on the board were
    # matched before this existed and would otherwise never see it.
    reattached = await _reattach_open_lines(session, product)

    await audit.record(
        session,
        entity_type="product",
        entity_id=product.id,
        action="variation_added",
        detail={"variation": variation.label, "options": options, "reattached": reattached},
        actor=user.username,
    )
    await session.commit()
    payload = _serialize(await _get(session, product_id))
    payload["added"] = {"label": variation.label, "reattached": reattached}
    return payload


class VariationRequest(BaseModel):
    """Every override is optional; null means "use the product's"."""

    bambuddy_archive_id: int | None = None
    bambuddy_archive_name: str | None = None
    bambuddy_file_path: str | None = None
    bambuddy_printer_id: int | None = None
    plate_number: int | None = Field(default=None, gt=0)
    units_per_plate: int | None = Field(default=None, gt=0)
    printer_models: list[str] = Field(default_factory=list, max_length=50)
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
    variation.bambuddy_file_path = (body.bambuddy_file_path or "").strip() or None
    variation.bambuddy_printer_id = body.bambuddy_printer_id
    variation.plate_number = body.plate_number
    variation.units_per_plate = body.units_per_plate
    variation.printer_models = (
        [] if body.bambuddy_printer_id is not None else _clean_models(body.printer_models)
    )
    variation.qbo_item_id = body.qbo_item_id
    variation.qbo_item_name = body.qbo_item_name
    variation.active = body.active
    await session.flush()

    await audit.record(
        session,
        entity_type="product",
        entity_id=product_id,
        action="variation_update",
        detail={
            "variation": variation.label,
            "archive": body.bambuddy_archive_id,
            "file_path": variation.bambuddy_file_path,
            "printer_id": body.bambuddy_printer_id,
        },
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


class VariantProductRequest(BaseModel):
    """Turn a variation into a product of its own."""

    name: str | None = Field(default=None, max_length=500)
    fulfillment: str = Field(pattern="^(printed|stocked|bundle)$")
    sku: str | None = Field(default=None, max_length=200)


@router.post("/{product_id}/variations/{variation_id}/product", status_code=201)
async def create_variant_product(
    product_id: uuid.UUID,
    variation_id: uuid.UUID,
    body: VariantProductRequest,
    user: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Give a variation its own product, so it can have its own components.

    "With fan" and "without fan" are not the same build, and a single BOM
    cannot describe both. The variant is a full product — its own BOM, print
    file and QuickBooks item — nested under the listing it is sold as.
    """
    master = await _get(session, product_id)
    variation = await session.get(ProductVariation, variation_id)
    if variation is None or variation.product_id != master.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variation not found")
    if variation.variant_product_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This variation already has its own product."
        )
    if master.parent_id is not None:
        # One level. A variant of a variant is a hierarchy nobody asked for and
        # every reader would then have to walk.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "This product is already a variant. Variants do not nest further.",
        )

    name = (body.name or f"{master.name} — {variation.label}").strip()[:500]
    code = (body.sku or "").strip() or await codes.unique(
        session,
        codes.from_listing(variation.etsy_listing_id, variation.etsy_product_id)
        or codes.from_name(name),
    )
    variant = Product(
        sku=code,
        name=name,
        parent_id=master.id,
        fulfillment=body.fulfillment,
        qbo_item_id=None,
        active=True,
    )
    session.add(variant)
    await session.flush()
    variation.variant_product_id = variant.id
    await session.flush()

    # Orders sitting on the master should move onto the variant, and take their
    # undispatched plates with them.
    moved = await _reattach_open_lines(session, master)

    await audit.record(
        session,
        entity_type="product",
        entity_id=master.id,
        action="variant_product_create",
        detail={"variation": variation.label, "variant": variant.sku, "moved": moved},
        actor=user.username,
    )
    await session.commit()
    payload = _serialize(await _get(session, product_id))
    payload["variant_product_id"] = str(variant.id)
    payload["moved"] = moved
    return payload


@router.delete("/{product_id}/variations/{variation_id}/product")
async def detach_variant_product(
    product_id: uuid.UUID,
    variation_id: uuid.UUID,
    _: User = Depends(require_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stop a variation resolving to its own product. The product itself stays."""
    variation = await session.get(ProductVariation, variation_id)
    if variation is None or variation.product_id != product_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Variation not found")
    variation.variant_product_id = None
    await session.flush()
    await session.commit()
    return _serialize(await _get(session, product_id))


async def _reattach_open_lines(session: AsyncSession, master: Product) -> int:
    """Re-resolve open lines sitting on a master or any of its variants."""
    # Queried rather than read off master.variants: the collection was loaded
    # before this request added a variant, and refreshing it mid-transaction is
    # a lazy load in the wrong place.
    family = [master.id] + list(
        (
            await session.execute(
                select(Product.id).where(Product.parent_id == master.id)
            )
        )
        .scalars()
        .all()
    )
    lines = (
        (
            await session.execute(
                select(OrderLine).where(
                    OrderLine.product_id.in_(family),
                    OrderLine.parent_line_id.is_(None),
                    OrderLine.state.notin_((LINE_SHIPPED, LINE_CANCELLED)),
                )
            )
        )
        .scalars()
        .all()
    )
    moved = 0
    for line in lines:
        before = line.product_id
        produced = await intake.resolve_line(session, line)
        if line.product_id != before:
            moved += 1
        await allocation.decide_lines(session, produced)
        await printing.plan_jobs(session, produced)
        order = await session.get(Order, line.order_id)
        if order is not None:
            await recompute_order(session, order)
    return moved
