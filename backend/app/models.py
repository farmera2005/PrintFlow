from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB on Postgres; plain JSON elsewhere so the unit-test suite can use SQLite.
JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

FULFILLMENT_TYPES = ("printed", "stocked", "bundle")

# Line-level states (see §5 of the build spec).
LINE_UNMATCHED = "unmatched"
LINE_NEW = "new"
LINE_EXPLODED = "exploded"
LINE_ALLOCATED = "allocated"
LINE_PRINTING = "printing"
LINE_PRINTED = "printed"
LINE_READY = "ready"
LINE_LABELED = "labeled"
LINE_SHIPPED = "shipped"
LINE_CANCELLED = "cancelled"

LINE_STATES = (
    LINE_UNMATCHED,
    LINE_NEW,
    LINE_EXPLODED,
    LINE_ALLOCATED,
    LINE_PRINTING,
    LINE_PRINTED,
    LINE_READY,
    LINE_LABELED,
    LINE_SHIPPED,
    LINE_CANCELLED,
)

# States in which a line still holds its soft stock reservation. A line releases
# its claim only once it ships or is cancelled.
RESERVING_LINE_STATES = tuple(
    s for s in LINE_STATES if s not in (LINE_SHIPPED, LINE_CANCELLED)
)

# Order-level roll-up statuses; these are the board columns, in order.
ORDER_NEW = "new"
ORDER_IN_PRODUCTION = "in_production"
ORDER_ASSEMBLY = "assembly"
ORDER_READY_TO_SHIP = "ready_to_ship"
ORDER_SHIPPED = "shipped"
ORDER_CANCELLED = "cancelled"

ORDER_STATUSES = (
    ORDER_NEW,
    ORDER_IN_PRODUCTION,
    ORDER_ASSEMBLY,
    ORDER_READY_TO_SHIP,
    ORDER_SHIPPED,
    ORDER_CANCELLED,
)

# Every column the board draws, Cancelled included: a card has to be draggable
# to any status, and one you cannot drag to is not a status the board offers.
BOARD_COLUMNS = ORDER_STATUSES

JOB_PENDING = "pending"
JOB_QUEUED = "queued"
JOB_PRINTING = "printing"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_STATUSES = (
    JOB_PENDING,
    JOB_QUEUED,
    JOB_PRINTING,
    JOB_DONE,
    JOB_FAILED,
    JOB_CANCELLED,
)

# Jobs the reconciler still needs to poll Bambuddy about.
JOB_OPEN_STATUSES = (JOB_PENDING, JOB_QUEUED, JOB_PRINTING)

SHEET_DRAFT = "draft"
SHEET_POSTED = "posted"
SHEET_VOIDED = "voided"
SHEET_STATUSES = (SHEET_DRAFT, SHEET_POSTED, SHEET_VOIDED)

PROVIDER_ETSY = "etsy"
PROVIDER_QBO = "qbo"
PROVIDER_BAMBUDDY = "bambuddy"
PROVIDER_SHIPSTATION = "shipstation"
PROVIDERS = (PROVIDER_ETSY, PROVIDER_QBO, PROVIDER_BAMBUDDY, PROVIDER_SHIPSTATION)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


class User(Base):
    """Single-admin local login. No roles, no multi-tenancy (§2)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class AppSetting(Base):
    """Key/value store for non-credential configuration (poll intervals etc.)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Any] = mapped_column(JsonType, nullable=False, default=dict)
    updated_at: Mapped[datetime] = _updated_at()


class Product(Base):
    __tablename__ = "products"

    id: Mapped[uuid.UUID] = _uuid_pk()
    sku: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # A variant of another product: "Bin, with fan" under "Storage bin". The
    # child is a product in its own right — its own BOM, print file and
    # QuickBooks item — because that is exactly how it differs. The parent is
    # what Etsy sells and what an order matches first.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    fulfillment: Mapped[str] = mapped_column(Text, nullable=False)
    qbo_item_id: Mapped[str | None] = mapped_column(Text)
    qbo_item_name: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint(
            "fulfillment in ('printed','stocked','bundle')", name="ck_products_fulfillment"
        ),
        CheckConstraint("parent_id is null or parent_id <> id", name="ck_products_parent_not_self"),
        Index("ix_products_sku_lower", func.lower(sku), unique=True),
    )

    bom_lines: Mapped[list[BomLine]] = relationship(
        back_populates="bundle",
        foreign_keys="BomLine.bundle_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    print_files: Mapped[list[PrintFile]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="PrintFile.created_at",
    )
    option_rules: Mapped[list[BomOptionRule]] = relationship(
        back_populates="bundle",
        foreign_keys="BomOptionRule.bundle_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    etsy_links: Mapped[list[EtsyProductLink]] = relationship(
        back_populates="product", cascade="all, delete-orphan", lazy="selectin"
    )
    variations: Mapped[list[ProductVariation]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        lazy="selectin",
        foreign_keys="ProductVariation.product_id",
    )
    variants: Mapped[list[Product]] = relationship(
        back_populates="parent", foreign_keys=[parent_id], lazy="selectin"
    )
    parent: Mapped[Product | None] = relationship(
        back_populates="variants", remote_side=[id], foreign_keys=[parent_id]
    )


class BomLine(Base):
    """One component of a bundle. Single-level only — enforced in validation."""

    __tablename__ = "bom_lines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    bundle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("bundle_id", "component_id", name="uq_bom_bundle_component"),
        CheckConstraint("quantity > 0", name="ck_bom_quantity_positive"),
        CheckConstraint("bundle_id <> component_id", name="ck_bom_no_self_reference"),
    )

    bundle: Mapped[Product] = relationship(
        back_populates="bom_lines", foreign_keys=[bundle_id]
    )
    component: Mapped[Product] = relationship(foreign_keys=[component_id], lazy="selectin")


class EtsyProductLink(Base):
    """An Etsy listing (or one variant of it) that means a given product.

    SKU matching only works when the seller put a SKU on the listing, and
    plenty of listings have none — Etsy does not require one. Those orders
    arrive with nothing to match on and stall as unmatched forever, which is no
    good when the shop plainly knows what the listing is.

    A link records that identity so the next order matches by itself:

    * `etsy_product_id` set — this exact variant of the listing.
    * `etsy_product_id` null — the whole listing, whatever the buyer picked.
      Usually the right one: options change the BOM through a rule, not the
      product, and Etsy regenerates product ids whenever the seller edits the
      listing's variations, so a listing id outlives a product id.
    """

    __tablename__ = "etsy_product_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    etsy_listing_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    etsy_product_id: Mapped[int | None] = mapped_column(BigInteger)
    # What the listing was called when the link was made, so the Products screen
    # can show something a human recognises rather than a bare number.
    listing_title: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        # One Etsy identity points at one product. Two partial indexes rather
        # than a single constraint, because a plain unique over both columns
        # would let a listing-wide link be added twice: NULLs never collide.
        Index(
            "uq_etsy_link_variant",
            "etsy_listing_id",
            "etsy_product_id",
            unique=True,
            postgresql_where=text("etsy_product_id is not null"),
            sqlite_where=text("etsy_product_id is not null"),
        ),
        Index(
            "uq_etsy_link_listing",
            "etsy_listing_id",
            unique=True,
            postgresql_where=text("etsy_product_id is null"),
            sqlite_where=text("etsy_product_id is null"),
        ),
    )

    product: Mapped[Product] = relationship(back_populates="etsy_links")


class ProductVariation(Base):
    """One buyable combination of a product's options, and what it changes.

    A listing sells "Storage Bin" with *Bin Fan: Yes* and *Bin Fan: No*. Those
    are one product to the shop and two different things to make — a different
    plate on the printer, or a different item drawn down in QuickBooks. Without
    somewhere to say so, every variation of a printed product would print the
    same file, silently.

    Matching an order to one of these is automatic, most specific first:

    * `etsy_product_id` — the exact variation Etsy sold. Unambiguous, but Etsy
      reissues these ids whenever the seller edits the listing's options.
    * `options` — the option names and values, compared case- and
      space-insensitively. Slower to match but survives an edit, which is why
      both are kept and the ids are refreshed rather than relied upon.

    What a matched variation then *is* comes in two weights:

    * `variant_product_id` — a product of its own, with its own BOM, print file
      and QuickBooks item. The right answer when the combinations are genuinely
      different things to make, which is most of the time.
    * the override columns below — a shortcut for when only the plate or the
      stock bucket differs and a whole product would be ceremony. Ignored when
      a variant product is set; that product carries everything.

    Both are optional. A variation that sets neither is still worth having: it
    records what the listing offers, and the order says which one was bought.
    """

    __tablename__ = "product_variations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    etsy_listing_id: Mapped[int | None] = mapped_column(BigInteger)
    etsy_product_id: Mapped[int | None] = mapped_column(BigInteger)
    # [{"name": "Bin Fan", "value": "Yes"}, …] — every option this combination
    # pins. An empty list matches nothing; that is a listing without variations,
    # which needs no variation row.
    options: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    label: Mapped[str] = mapped_column(Text, nullable=False)

    # The product this combination actually is. RESTRICT rather than CASCADE:
    # deleting the variant product is a catalogue decision that should be made
    # deliberately, not fall out of tidying up a variation row.
    variant_product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )

    # Print override, used only when there is no variant product. Null archive
    # means "use the product's print mapping".
    bambuddy_archive_id: Mapped[int | None] = mapped_column(Integer)
    bambuddy_archive_name: Mapped[str | None] = mapped_column(Text)
    bambuddy_file_path: Mapped[str | None] = mapped_column(Text)
    bambuddy_printer_id: Mapped[int | None] = mapped_column(Integer)
    plate_number: Mapped[int | None] = mapped_column(Integer)
    units_per_plate: Mapped[int | None] = mapped_column(Integer)
    # Empty means "whatever the product's mapping says".
    printer_models: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )

    # Stock override. Null means "use the product's QuickBooks item".
    qbo_item_id: Mapped[str | None] = mapped_column(Text)
    qbo_item_name: Mapped[str | None] = mapped_column(Text)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint(
            "units_per_plate is null or units_per_plate > 0",
            name="ck_variation_units_per_plate_positive",
        ),
        CheckConstraint(
            "plate_number is null or plate_number > 0",
            name="ck_variation_plate_positive",
        ),
        # One row per Etsy variation. Partial, because plenty of variations are
        # described by their options alone and carry no id at all.
        Index(
            "uq_variation_etsy_product",
            "etsy_product_id",
            unique=True,
            postgresql_where=text("etsy_product_id is not null"),
            sqlite_where=text("etsy_product_id is not null"),
        ),
    )

    product: Mapped[Product] = relationship(
        back_populates="variations", foreign_keys=[product_id]
    )
    variant_product: Mapped[Product | None] = relationship(
        foreign_keys=[variant_product_id], lazy="selectin"
    )


class BomOptionRule(Base):
    """How one chosen Etsy option changes a bundle's BOM.

    A buyer picking "Color: Red" does not change what the shop sells, it changes
    what comes off the shelf — red filament instead of grey. Modelling that as a
    separate product per colour would split the finished good in QuickBooks for
    no reason, so the option edits the BOM instead.

    Two shapes cover what shops actually need:

    * `replaces_id` set — swap that component for `component_id`. The quantity
      carries over from the BOM line unless `quantity` overrides it.
    * `replaces_id` null — add `component_id` × `quantity`, for options that add
      something rather than change it ("Gift box: Yes").
    """

    __tablename__ = "bom_option_rules"

    id: Mapped[uuid.UUID] = _uuid_pk()
    bundle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Matched against Etsy's formatted_name / formatted_value, trimmed and
    # case-insensitively — the same forgiveness SKU matching already gets,
    # because these strings are typed by hand in the Etsy listing editor.
    option_name: Mapped[str] = mapped_column(Text, nullable=False)
    option_value: Mapped[str] = mapped_column(Text, nullable=False)

    replaces_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT")
    )
    component_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint(
            "quantity is null or quantity > 0", name="ck_bom_option_quantity_positive"
        ),
        CheckConstraint(
            "replaces_id is null or replaces_id <> component_id",
            name="ck_bom_option_no_self_swap",
        ),
        Index(
            "uq_bom_option_rule",
            "bundle_id",
            func.lower(option_name),
            func.lower(option_value),
            "component_id",
            unique=True,
        ),
    )

    bundle: Mapped[Product] = relationship(
        back_populates="option_rules", foreign_keys=[bundle_id]
    )
    component: Mapped[Product] = relationship(foreign_keys=[component_id], lazy="selectin")
    replaces: Mapped[Product | None] = relationship(
        foreign_keys=[replaces_id], lazy="selectin"
    )


class PrintFile(Base):
    """One file a product can be printed from, and the machines it is for.

    A product has as many of these as it has ways of being made. A farm whose
    library is sorted by printer model has one file per model — the same part,
    sliced differently — and which one gets used is a question about which
    machine is free, answered when the plate is actually sent rather than
    guessed at when it is planned.

    The table is still `print_mappings`, from when a product had exactly one.
    """

    __tablename__ = "print_mappings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Two ways to say the same thing, because instances differ on which they
    # accept: the archive's id, and its path in the file manager. A file picked
    # out of the file manager has a path and may have no id at all, so neither
    # can be required — but one of them has to be there, which the check
    # constraint enforces.
    bambuddy_archive_id: Mapped[int | None] = mapped_column(Integer)
    bambuddy_archive_name: Mapped[str | None] = mapped_column(Text)
    bambuddy_file_path: Mapped[str | None] = mapped_column(Text)
    # The machine this file was picked from, when it was picked from one
    # machine's file manager rather than the farm-wide library. It is where the
    # plate goes: a file that lives on one printer cannot be printed elsewhere.
    bambuddy_printer_id: Mapped[int | None] = mapped_column(Integer)
    plate_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    units_per_plate: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    print_options: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    # Which printer models can make this — ["P1S", "X1C"]. A job is described by
    # what is capable of it, not by which machine happened to be free, and a
    # farm usually has several that qualify. Empty means any of them; the
    # printer itself is chosen when the plate is actually sent.
    printer_models: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("units_per_plate > 0", name="ck_mapping_units_per_plate_positive"),
        CheckConstraint("plate_number > 0", name="ck_mapping_plate_positive"),
        CheckConstraint(
            "bambuddy_archive_id IS NOT NULL OR bambuddy_file_path IS NOT NULL",
            name="ck_mapping_has_a_source",
        ),
    )

    product: Mapped[Product] = relationship(back_populates="print_files")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    etsy_receipt_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    order_number: Mapped[str] = mapped_column(Text, nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The column the board draws this card in, and the operator's to set. Never
    # written by the roll-up — see services/state.py.
    status: Mapped[str] = mapped_column(Text, nullable=False, default=ORDER_NEW)
    # Why it was moved, when somebody bothered to say.
    status_note: Mapped[str | None] = mapped_column(Text)

    shipstation_order_id: Mapped[int | None] = mapped_column(BigInteger)
    shipstation_last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    shipstation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tracking_number: Mapped[str | None] = mapped_column(Text)
    carrier_code: Mapped[str | None] = mapped_column(Text)
    service_code: Mapped[str | None] = mapped_column(Text)
    label_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label_pdf: Mapped[bytes | None] = mapped_column(LargeBinary)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint(
            "status in ('new','in_production','assembly','ready_to_ship','shipped','cancelled')",
            name="ck_orders_status",
        ),
        Index("ix_orders_status", "status"),
    )

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLine(Base):
    __tablename__ = "order_lines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_line_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("order_lines.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )
    # Which of the product's variations this line is, worked out at intake from
    # the options the buyer picked. Decides the plate that gets printed and the
    # QuickBooks item drawn down, when the variation overrides them.
    variation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("product_variations.id", ondelete="SET NULL"), index=True
    )
    etsy_listing_id: Mapped[int | None] = mapped_column(BigInteger)
    # Etsy's inventory product: the exact variant the buyer bought. Kept so a
    # listing with no SKU can still be matched, and so a link can be narrowed
    # to one variant.
    etsy_product_id: Mapped[int | None] = mapped_column(BigInteger)
    etsy_transaction_id: Mapped[int | None] = mapped_column(BigInteger)
    sku_raw: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    # The options the buyer chose on Etsy, normalised to
    # [{"name": "Color", "value": "Red", ...}]. Kept on the line rather than
    # only in the receipt payload because they decide what gets made: a colour
    # choice can swap a component out of the BOM.
    variations: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    # Which option rules fired, in words. Recomputed on every intake run, and
    # kept because "why is this order pulling red filament" is a question that
    # gets asked after the fact, when the rules may already have changed.
    option_effects: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    qty_from_stock: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qty_to_print: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state: Mapped[str] = mapped_column(Text, nullable=False, default=LINE_NEW)

    # Manual override recorded by the operator ("mark printed" for an off-Bambuddy
    # print, "cancel line"). When set it wins over the computed state.
    override_state: Mapped[str | None] = mapped_column(Text)
    # "Skip stock, print anyway" — decisioning ignores QBO for this line.
    force_print: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Assembly check-off, only meaningful on bundle container lines.
    assembled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stock_note: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_lines_quantity_positive"),
        CheckConstraint("qty_from_stock >= 0", name="ck_order_lines_stock_nonneg"),
        CheckConstraint("qty_to_print >= 0", name="ck_order_lines_print_nonneg"),
        Index("ix_order_lines_state", "state"),
    )

    order: Mapped[Order] = relationship(back_populates="lines")
    children: Mapped[list[OrderLine]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped[OrderLine | None] = relationship(
        back_populates="children", remote_side=[id]
    )
    product: Mapped[Product | None] = relationship(lazy="selectin")
    # Eager, like the product: the board reads it for every line it draws, and a
    # lazy load there is a MissingGreenlet waiting to happen.
    variation: Mapped[ProductVariation | None] = relationship(lazy="selectin")
    print_jobs: Mapped[list[PrintJob]] = relationship(
        back_populates="order_line", cascade="all, delete-orphan"
    )


class PrintJob(Base):
    """One row per Bambuddy queue item — one plate."""

    __tablename__ = "print_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order_lines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    bambuddy_queue_id: Mapped[int | None] = mapped_column(Integer)
    # What to print, copied off the mapping when the plate was planned. Either
    # of these identifies the file; see PrintMapping for why there are two.
    bambuddy_archive_id: Mapped[int | None] = mapped_column(Integer)
    bambuddy_file_path: Mapped[str | None] = mapped_column(Text)
    plate_number: Mapped[int | None] = mapped_column(Integer)
    # The machine this actually went to, chosen when it was sent — or fixed in
    # advance, when the file only exists on one printer.
    printer_id: Mapped[int | None] = mapped_column(Integer)
    # The models that could have taken it, recorded when the plate was planned.
    # Kept on the job so dispatch does not have to re-derive it, and so a job
    # that cannot find a machine can say what it was looking for.
    printer_models: Mapped[list[str]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    # Every file this plate could be printed from, one per way the product can
    # be made. Which one is used is a question about which machine is free, and
    # that is not knowable when the plate is planned — so the choice is carried
    # here and made at dispatch, where the answer is. The fields above record
    # what was chosen once it has been.
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonType, nullable=False, default=list
    )
    units_expected: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=JOB_PENDING)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint(
            "status in ('pending','queued','printing','done','failed','cancelled')",
            name="ck_print_jobs_status",
        ),
        Index("ix_print_jobs_status", "status"),
    )

    order_line: Mapped[OrderLine] = relationship(back_populates="print_jobs")

    @property
    def file_label(self) -> str | None:
        """What to call the file this plate is using, for a person reading it.

        Two plates of the same product are no longer the same plate — one may be
        the H2D slicing and the next the P1S — so the product's name no longer
        says what is printing. The candidate the plate settled on carries the
        name it was picked under; failing that, the path, failing that the id.
        """
        for candidate in self.candidates or []:
            if (
                candidate.get("archive_id") == self.bambuddy_archive_id
                and candidate.get("file_path") == self.bambuddy_file_path
                and candidate.get("name")
            ):
                return str(candidate["name"])
        if self.bambuddy_file_path:
            return self.bambuddy_file_path.rsplit("/", 1)[-1]
        return f"archive {self.bambuddy_archive_id}" if self.bambuddy_archive_id else None


class MadeSheet(Base):
    """A batch of items manufactured into stock.

    Posting one writes a single Purchase (an Expense) to QuickBooks: item lines
    for what was made, and — where the product has a BOM — negative item lines
    for the components it consumed. QuickBooks raises quantity on hand for the
    positive lines and lowers it for the negative ones, so the sheet moves value
    from components into finished goods in one transaction.

    Sheets are drafts until posted. Nothing reaches QuickBooks without someone
    pressing Post, and a posted sheet is immutable — correcting one means
    voiding it and making another, the same as in any ledger.
    """

    __tablename__ = "made_sheets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    made_on: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default=SHEET_DRAFT)

    qbo_purchase_id: Mapped[str | None] = mapped_column(Text)
    qbo_doc_number: Mapped[str | None] = mapped_column(Text)
    qbo_sync_token: Mapped[str | None] = mapped_column(Text)
    # What was actually sent and what came back, kept verbatim. When a posting
    # is questioned months later the payload is the only reliable answer.
    qbo_request: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    qbo_response: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    # Stable across retries so a timeout followed by a retry cannot post twice.
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, default=uuid.uuid4
    )

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    posted_by: Mapped[str | None] = mapped_column(Text)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','posted','voided')", name="ck_made_sheets_status"
        ),
        Index("ix_made_sheets_status", "status"),
    )

    lines: Mapped[list[MadeSheetLine]] = relationship(
        back_populates="sheet",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="MadeSheetLine.created_at",
    )


class MadeSheetLine(Base):
    """One thing made, in a quantity, at a unit cost.

    A line names either a PrintFlow product or a QuickBooks item directly.
    Plenty of stock is worth counting into QuickBooks without ever being a
    product here — supplies, sub-assemblies, anything not sold on Etsy — so the
    sheet can reach straight into the QuickBooks item list.

    When a chosen QuickBooks item *is* mapped to a product, the line is stored
    against the product. Otherwise the same physical act would post differently
    depending on which picker was used: no BOM, so no components consumed.
    """

    __tablename__ = "made_sheet_lines"

    id: Mapped[uuid.UUID] = _uuid_pk()
    sheet_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("made_sheets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # RESTRICT: a product named in a posted sheet is part of the accounting
    # record and must not vanish from under it.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT")
    )
    # Set instead of product_id when the line came from the QuickBooks list and
    # no product maps to it. The name is a snapshot for display; the id is what
    # posts.
    qbo_item_id: Mapped[str | None] = mapped_column(Text)
    qbo_item_name: Mapped[str | None] = mapped_column(Text)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # Numeric, not float: this is money and it is going into someone's books.
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False, default=0)
    # True while the cost is still the suggested one — a BOM roll-up, or the
    # item's cost in QuickBooks. Cleared once someone types over it, so a later
    # BOM change does not silently overwrite their figure.
    cost_from_bom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_made_sheet_line_quantity_positive"),
        CheckConstraint("unit_cost >= 0", name="ck_made_sheet_line_cost_not_negative"),
        CheckConstraint(
            "(product_id is not null) <> (qbo_item_id is not null)",
            name="ck_made_sheet_line_one_target",
        ),
        # Partial indexes rather than one UniqueConstraint: a plain unique over
        # both columns would let the same thing appear twice, once per column,
        # because NULLs never collide.
        Index(
            "uq_made_sheet_line_product",
            "sheet_id",
            "product_id",
            unique=True,
            postgresql_where=text("product_id is not null"),
            sqlite_where=text("product_id is not null"),
        ),
        Index(
            "uq_made_sheet_line_qbo_item",
            "sheet_id",
            "qbo_item_id",
            unique=True,
            postgresql_where=text("qbo_item_id is not null"),
            sqlite_where=text("qbo_item_id is not null"),
        ),
    )

    sheet: Mapped[MadeSheet] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship(lazy="selectin")

    # ----------------------------------------------------------------
    # One shape for both kinds of line, so callers never branch on which
    # column happens to be set.
    # ----------------------------------------------------------------

    @property
    def item_id(self) -> str | None:
        """The QuickBooks item this line moves, however the line was made."""
        return self.product.qbo_item_id if self.product else self.qbo_item_id

    @property
    def item_label(self) -> str:
        """What to call this line in an error message or a description."""
        if self.product:
            return self.product.sku
        return self.qbo_item_name or f"QuickBooks item {self.qbo_item_id}"

    @property
    def item_name(self) -> str:
        if self.product:
            return self.product.name
        return self.qbo_item_name or ""

    @property
    def bom_lines(self) -> list[BomLine]:
        """Components consumed. A bare QuickBooks item has none."""
        return list(self.product.bom_lines) if self.product else []


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"

    id: Mapped[uuid.UUID] = _uuid_pk()
    provider: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class TlsCertificate(Base):
    """The HTTPS certificate, generated or uploaded during setup.

    The private key is encrypted with the same key-derivation as integration
    credentials. Keeping the pair in the database (rather than only on disk)
    means a rebuilt container comes back up on the same certificate.
    """

    __tablename__ = "tls_certificates"

    id: Mapped[uuid.UUID] = _uuid_pk()
    cert_pem: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_key: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    common_name: Mapped[str | None] = mapped_column(Text)
    sans: Mapped[list[str] | None] = mapped_column(JsonType)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    self_signed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="generated")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("source in ('generated','uploaded')", name="ck_tls_source"),
        Index("ix_tls_certificates_active", "active"),
    )


class SyncLog(Base):
    __tablename__ = "sync_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    detail: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Every manual override lands here (§5)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    entity_type: Mapped[str] = mapped_column(Text, nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JsonType)
    actor: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class OAuthState(Base):
    """Short-lived CSRF state + PKCE verifier for an in-flight OAuth handshake."""

    __tablename__ = "oauth_states"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    code_verifier: Mapped[str | None] = mapped_column(Text)
    redirect_uri: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
