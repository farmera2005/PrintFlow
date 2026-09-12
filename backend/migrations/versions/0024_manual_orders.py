"""a line remembers what it cost

An order typed in by hand has no channel payload behind it, so there is nowhere
to read a line's price back out of — and an invoice needs one per line. The
price moves onto the line itself, where a manual order actually knows it.

Null for every existing row, which is right: a polled order's price still comes
out of the receipt or the Wix order it kept, and nothing about that changes.

Revision ID: 0024_manual_orders
Revises: 0023_wix
Create Date: 2026-09-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0024_manual_orders'
down_revision = '0023_wix'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Four decimal places, like every other unit figure here: a price divided
    # by a quantity is not always a round number of cents, and rounding on the
    # way in would put an error into the invoice rather than onto the screen.
    op.add_column(
        'order_lines',
        sa.Column('unit_price', sa.Numeric(12, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('order_lines', 'unit_price')
