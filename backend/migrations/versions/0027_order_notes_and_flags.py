"""a note and some flags on an order

Everything an order carries so far is a fact some system reported — what Etsy
sold, what QuickBooks was told, what the carrier last said. None of it has
anywhere to put the sentence a person would otherwise write on a sticky note
and press to a monitor, and none of it answers "this one is the rush job".

So: free text, and a list of the shop's own flag ids. The flags' labels and
colours are not here — they live in the `order_flags` app setting, because a
shop renaming "Rush" to "Priority" should be renaming it in one place rather
than on every order that carries it.

Revision ID: 0027_order_notes_and_flags
Revises: 0026_production_priority
Create Date: 2026-11-19
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0027_order_notes_and_flags'
down_revision = '0026_production_priority'
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.add_column('orders', sa.Column('notes', sa.Text(), nullable=True))
    # No index. The only question asked of this column is "how many orders
    # carry this flag", asked once when somebody opens Settings and about to
    # delete one, over a table a single shop fills at the speed it sells
    # things. An index would be paid for on every write to earn nothing.
    op.add_column(
        'orders',
        sa.Column('flags', JSON_TYPE, nullable=False, server_default='[]'),
    )


def downgrade() -> None:
    op.drop_column('orders', 'flags')
    op.drop_column('orders', 'notes')
