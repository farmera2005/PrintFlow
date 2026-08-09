"""an order status an operator can set by hand

Revision ID: 0009_order_status_override
Revises: 0008_variant_products
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0009_order_status_override'
down_revision = '0008_variant_products'
branch_labels = None
depends_on = None

STATUSES = "'new','in_production','assembly','ready_to_ship','shipped','cancelled'"


def upgrade() -> None:
    op.add_column('orders', sa.Column('status_override', sa.Text(), nullable=True))
    op.add_column('orders', sa.Column('status_override_note', sa.Text(), nullable=True))
    op.create_check_constraint(
        'ck_orders_status_override',
        'orders',
        f'status_override is null or status_override in ({STATUSES})',
    )


def downgrade() -> None:
    op.drop_constraint('ck_orders_status_override', 'orders', type_='check')
    op.drop_column('orders', 'status_override_note')
    op.drop_column('orders', 'status_override')
