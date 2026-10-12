"""files on an order

A customer's model, a photo of the proof they approved, the purchase order a
trade buyer sent in. All of it currently lives in somebody's email, which is
the one place the shop floor cannot reach.

The row is the index card: who put it there, what it was called, how big it
was and what it is for. The bytes live under the data directory, which is the
half of a backup that is carried as files rather than as base64 inside the
database dump.

Revision ID: 0025_order_files
Revises: 0024_manual_orders
Create Date: 2026-10-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0025_order_files'
down_revision = '0024_manual_orders'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'order_attachments',
        sa.Column('id', sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            'order_id', sa.Uuid(as_uuid=True),
            sa.ForeignKey('orders.id', ondelete='CASCADE'), nullable=False,
        ),
        # What the person called it. Shown and sent back on download, never
        # used to build a path.
        sa.Column('filename', sa.Text(), nullable=False),
        # Where the bytes are, relative to the data directory — so moving the
        # volume does not mean rewriting every row.
        sa.Column('stored_path', sa.Text(), nullable=False),
        sa.Column('content_type', sa.Text(), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default='0'),
        sa.Column('sha256', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('uploaded_by', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint('size_bytes >= 0', name='ck_order_attachments_size_nonneg'),
    )
    op.create_index('ix_order_attachments_order', 'order_attachments', ['order_id'])


def downgrade() -> None:
    # The rows go; the files under the data directory do not. Deleting a
    # shop's uploads to undo a schema change is not a trade a migration gets
    # to make on its own.
    op.drop_index('ix_order_attachments_order', table_name='order_attachments')
    op.drop_table('order_attachments')
