"""a Complete column, and what the carrier says about the parcel

An order that has shipped is not finished — it is in a van. The board had
nowhere to put the difference, so Shipped was both "posted this morning" and
"arrived last Tuesday" and the column only ever grew.

So: a Complete column an order reaches on its own when the carrier says it was
delivered, and the readings behind that decision kept on the order rather than
fetched for every board refresh. `completed_at` is the board's bookkeeping —
when the card entered the column, however it got there — and is what the
48-hour clock runs from. `delivered_at` is the fact the carrier stated. They
differ whenever somebody moves a card by hand, which is why they are two
columns rather than one.

Revision ID: 0016_delivery_tracking
Revises: 0015_order_finances
Create Date: 2026-08-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0016_delivery_tracking'
down_revision = '0015_order_finances'
branch_labels = None
depends_on = None

OLD_STATUSES = "'new','in_production','assembly','ready_to_ship','shipped','cancelled'"
NEW_STATUSES = (
    "'new','in_production','assembly','ready_to_ship','shipped','complete','cancelled'"
)


def upgrade() -> None:
    op.add_column('orders', sa.Column('tracking_status', sa.Text(), nullable=True))
    op.add_column('orders', sa.Column('tracking_detail', sa.Text(), nullable=True))
    op.add_column('orders', sa.Column('tracking_url', sa.Text(), nullable=True))
    op.add_column(
        'orders',
        sa.Column('tracking_status_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'orders',
        sa.Column('tracking_checked_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'orders',
        sa.Column(
            'tracking_attempts', sa.Integer(), nullable=False, server_default='0'
        ),
    )
    op.add_column(
        'orders', sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'orders', sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True)
    )
    # Widen the column list before anything can be written into it.
    op.drop_constraint('ck_orders_status', 'orders', type_='check')
    op.create_check_constraint(
        'ck_orders_status', 'orders', f'status in ({NEW_STATUSES})'
    )


def downgrade() -> None:
    # Nothing can be in a column that is about to stop existing. These orders
    # were delivered, so Shipped is where they were before Complete existed and
    # where they belong again — not Cancelled, and not lost.
    op.execute("update orders set status = 'shipped' where status = 'complete'")
    op.drop_constraint('ck_orders_status', 'orders', type_='check')
    op.create_check_constraint(
        'ck_orders_status', 'orders', f'status in ({OLD_STATUSES})'
    )
    op.drop_column('orders', 'completed_at')
    op.drop_column('orders', 'delivered_at')
    op.drop_column('orders', 'tracking_attempts')
    op.drop_column('orders', 'tracking_checked_at')
    op.drop_column('orders', 'tracking_status_at')
    op.drop_column('orders', 'tracking_url')
    op.drop_column('orders', 'tracking_detail')
    op.drop_column('orders', 'tracking_status')
