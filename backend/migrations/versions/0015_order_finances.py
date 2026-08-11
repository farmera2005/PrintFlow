"""where an order is going, what it was worth, and what Etsy took

The receipt PrintFlow already stores carries the address and every figure the
buyer paid; none of it was ever read out of the payload. Fees are not in the
receipt at all — they arrive later, on the shop's payment ledger — so they get
their own columns and a timestamp saying when they were last looked up.

Revision ID: 0015_order_finances
Revises: 0014_label_cost
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

revision = '0015_order_finances'
down_revision = '0014_label_cost'
branch_labels = None
depends_on = None

MONEY = ('revenue', 'items_total', 'shipping_total', 'tax_total', 'discount_total',
         'etsy_fees', 'marketing_fees', 'processing_fees')


def upgrade() -> None:
    op.add_column('orders', sa.Column('ship_to', JSON_TYPE, nullable=True))
    op.add_column('orders', sa.Column('currency', sa.Text(), nullable=True))
    for name in MONEY:
        op.add_column('orders', sa.Column(name, sa.Numeric(12, 4), nullable=True))
    op.add_column('orders', sa.Column('fee_lines', JSON_TYPE, nullable=True))
    op.add_column(
        'orders',
        sa.Column('finance_synced_at', sa.DateTime(timezone=True), nullable=True),
    )
    # Fees are stated as positive amounts taken off the top, so a negative one
    # is a bug rather than a refund. Revenue is left unconstrained: a fully
    # refunded order can legitimately be worth nothing, and a correction can
    # take it below.
    op.create_check_constraint(
        'ck_orders_fees_not_negative',
        'orders',
        'etsy_fees >= 0 and marketing_fees >= 0 and processing_fees >= 0',
    )


def downgrade() -> None:
    op.drop_constraint('ck_orders_fees_not_negative', 'orders', type_='check')
    op.drop_column('orders', 'finance_synced_at')
    op.drop_column('orders', 'fee_lines')
    for name in reversed(MONEY):
        op.drop_column('orders', name)
    op.drop_column('orders', 'currency')
    op.drop_column('orders', 'ship_to')
