"""which QuickBooks item a chosen option is sold as

One listing is often several things in QuickBooks. A playset sold in HO and 1:64
is two items on the books, and the buyer picking a scale is what says which — so
the mapping belongs on the option rather than on the product, and a product
carries as many of these as it has options worth telling apart.

Not the same table as bom_option_rules, which answers a different question about
the same option: that one says what comes off the shelf to make the thing, this
one says what the thing is sold as. A colour usually changes the first and not
the second; a scale usually changes both.

Revision ID: 0018_option_items
Revises: 0017_books
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0018_option_items'
down_revision = '0017_books'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'product_option_items',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column(
            'product_id',
            sa.Uuid(),
            sa.ForeignKey('products.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('option_name', sa.Text(), nullable=False),
        sa.Column('option_value', sa.Text(), nullable=False),
        sa.Column('qbo_item_id', sa.Text(), nullable=False),
        sa.Column('qbo_item_name', sa.Text(), nullable=True),
        # Which mapping wins when a buyer's choices match more than one.
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('now()'),
        ),
        sa.UniqueConstraint(
            'product_id', 'option_name', 'option_value', name='uq_option_item_choice'
        ),
    )
    op.create_index(
        'ix_product_option_items_product_id', 'product_option_items', ['product_id']
    )


def downgrade() -> None:
    op.drop_index('ix_product_option_items_product_id', table_name='product_option_items')
    op.drop_table('product_option_items')
