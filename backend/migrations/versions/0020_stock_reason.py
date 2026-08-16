"""why a line's stock was taken out

Two different events now book a removal against a line: a print finishing, and
a bundle being marked assembled. Undoing one of them must not unwind the other
— clearing an assembly should not put back units that a print consumed — and
the row cannot say which happened without being asked to.

Existing removals were all made by printing, which is the only thing that made
them before this release, so that is what they are backfilled as.

Revision ID: 0020_stock_reason
Revises: 0019_option_item_sets
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0020_stock_reason'
down_revision = '0019_option_item_sets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('order_lines', sa.Column('qbo_stock_reason', sa.Text(), nullable=True))
    op.execute(
        """
        update order_lines
           set qbo_stock_reason = 'printed'
         where qbo_stock_removed_at is not null
        """
    )


def downgrade() -> None:
    op.drop_column('order_lines', 'qbo_stock_reason')
