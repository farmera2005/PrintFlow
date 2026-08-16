"""what an order cost, expensed into QuickBooks

Two bills arrive against an order and neither was reaching the books: the
carrier's, when a label is bought, and Etsy's, when its ledger settles. Each
becomes its own Purchase, so each needs its own once-only proof — the id, the
token needed to delete it again, what it came to, and why the last attempt
failed.

`fees_source` records whether the fee figures were swept out of Etsy's ledger
or typed by a person because it had not settled yet. Existing rows carry
whatever the sweep found, which is the only thing that could have set them.

Revision ID: 0021_order_expenses
Revises: 0020_stock_reason
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0021_order_expenses'
down_revision = '0020_stock_reason'
branch_labels = None
depends_on = None

MONEY = sa.Numeric(12, 4)


def upgrade() -> None:
    for prefix in ('qbo_shipping_expense', 'qbo_fee_expense'):
        op.add_column('orders', sa.Column(f'{prefix}_id', sa.Text(), nullable=True))
        op.add_column('orders', sa.Column(f'{prefix}_sync_token', sa.Text(), nullable=True))
        op.add_column(
            'orders',
            sa.Column(f'{prefix}_at', sa.DateTime(timezone=True), nullable=True),
        )
        op.add_column('orders', sa.Column(f'{prefix}_total', MONEY, nullable=True))
        op.add_column('orders', sa.Column(f'{prefix}_error', sa.Text(), nullable=True))

    op.add_column('orders', sa.Column('fees_source', sa.Text(), nullable=True))
    # Everything that has fees on it now got them from the sweep; nothing else
    # could have set them before this release.
    op.execute(
        """
        update orders
           set fees_source = 'etsy'
         where etsy_fees is not null
            or marketing_fees is not null
            or processing_fees is not null
        """
    )


def downgrade() -> None:
    op.drop_column('orders', 'fees_source')
    for prefix in ('qbo_shipping_expense', 'qbo_fee_expense'):
        for suffix in ('id', 'sync_token', 'at', 'total', 'error'):
            op.drop_column('orders', f'{prefix}_{suffix}')
