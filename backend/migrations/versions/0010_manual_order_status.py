"""order status belongs to the operator, not the roll-up

Revision ID: 0010_manual_order_status
Revises: 0009_order_status_override
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0010_manual_order_status'
down_revision = '0009_order_status_override'
branch_labels = None
depends_on = None

STATUSES = "'new','in_production','assembly','ready_to_ship','shipped','cancelled'"


def upgrade() -> None:
    # `status` already holds the effective value — it was written as
    # `override or derived` — so dropping the override loses nothing.
    op.drop_constraint('ck_orders_status_override', 'orders', type_='check')
    op.drop_column('orders', 'status_override')
    op.alter_column('orders', 'status_override_note', new_column_name='status_note')


def downgrade() -> None:
    op.alter_column('orders', 'status_note', new_column_name='status_override_note')
    op.add_column('orders', sa.Column('status_override', sa.Text(), nullable=True))
    op.create_check_constraint(
        'ck_orders_status_override',
        'orders',
        f'status_override is null or status_override in ({STATUSES})',
    )
