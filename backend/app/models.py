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

BOARD_COLUMNS = (
    ORDER_NEW,
    ORDER_IN_PRODUCTION,
    ORDER_ASSEMBLY,
    ORDER_READY_TO_SHIP,
    ORDER_SHIPPED,
)

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
        Index("ix_products_sku_lower", func.lower(sku), unique=True),
    )

    bom_lines: Mapped[list[BomLine]] = relationship(
        back_populates="bundle",
        foreign_keys="BomLine.bundle_id",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    print_mapping: Mapped[PrintMapping | None] = relationship(
        back_populates="product", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )
    option_rules: Mapped[list[BomOptionRule]] = relationship(
        back_populates="bundle",
        foreign_keys="BomOptionRule.bundle_id",
        cascade="all, delete-orphan",
        lazy="selectin",
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


class PrintMapping(Base):
    """SKU → archived 3MF + plate. One mapping per printed SKU."""

    __tablename__ = "print_mappings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    bambuddy_archive_id: Mapped[int] = mapped_column(Integer, nullable=False)
    bambuddy_archive_name: Mapped[str | None] = mapped_column(Text)
    plate_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    units_per_plate: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    print_options: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    preferred_printer_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("units_per_plate > 0", name="ck_mapping_units_per_plate_positive"),
        CheckConstraint("plate_number > 0", name="ck_mapping_plate_positive"),
    )

    product: Mapped[Product] = relationship(back_populates="print_mapping")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    etsy_receipt_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    order_number: Mapped[str] = mapped_column(Text, nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default=ORDER_NEW)

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
    etsy_listing_id: Mapped[int | None] = mapped_column(BigInteger)
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
    bambuddy_archive_id: Mapped[int] = mapped_column(Integer, nullable=False)
    plate_number: Mapped[int | None] = mapped_column(Integer)
    printer_id: Mapped[int | None] = mapped_column(Integer)
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
