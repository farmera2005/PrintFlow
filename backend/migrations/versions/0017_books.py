"""what PrintFlow writes into QuickBooks from the order pipeline

Two writes that did not exist before, and the columns that make each of them
happen exactly once.

A printed line takes its units out of QuickBooks stock. Line states are derived
and recomputed from scratch on every call, so "is this line printed" is not a
safe question to hang a book entry on — it is true again on the next recompute.
`qbo_stock_removed_at` is the fact that survives, and the Purchase id beside it
is what lets the entry be taken back if the line is later cancelled.

An order can be invoiced. `qbo_invoice_id` is what makes a second press of the
button say "already invoiced" instead of putting a second document in somebody's
books.

Both errors are columns rather than log lines: a book entry that did not happen
is something a person has to see and retry.

Revision ID: 0017_books
Revises: 0016_delivery_tracking
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0017_books'
down_revision = '0016_delivery_tracking'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'order_lines',
        sa.Column('qbo_stock_removed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'order_lines', sa.Column('qbo_stock_purchase_id', sa.Text(), nullable=True)
    )
    op.add_column(
        'order_lines', sa.Column('qbo_stock_sync_token', sa.Text(), nullable=True)
    )
    op.add_column('order_lines', sa.Column('qbo_stock_qty', sa.Integer(), nullable=True))
    op.add_column('order_lines', sa.Column('qbo_stock_error', sa.Text(), nullable=True))

    op.add_column('orders', sa.Column('qbo_invoice_id', sa.Text(), nullable=True))
    op.add_column(
        'orders', sa.Column('qbo_invoice_doc_number', sa.Text(), nullable=True)
    )
    op.add_column(
        'orders', sa.Column('qbo_invoice_at', sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        'orders', sa.Column('qbo_invoice_total', sa.Numeric(12, 4), nullable=True)
    )
    op.add_column('orders', sa.Column('qbo_customer_id', sa.Text(), nullable=True))
    op.add_column('orders', sa.Column('qbo_invoice_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('orders', 'qbo_invoice_error')
    op.drop_column('orders', 'qbo_customer_id')
    op.drop_column('orders', 'qbo_invoice_total')
    op.drop_column('orders', 'qbo_invoice_at')
    op.drop_column('orders', 'qbo_invoice_doc_number')
    op.drop_column('orders', 'qbo_invoice_id')

    op.drop_column('order_lines', 'qbo_stock_error')
    op.drop_column('order_lines', 'qbo_stock_qty')
    op.drop_column('order_lines', 'qbo_stock_sync_token')
    op.drop_column('order_lines', 'qbo_stock_purchase_id')
    op.drop_column('order_lines', 'qbo_stock_removed_at')
