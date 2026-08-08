from __future__ import annotations

import uuid
from datetime import datetime
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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
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
