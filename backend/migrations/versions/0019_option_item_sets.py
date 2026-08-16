"""a sold-as mapping can pin more than one option

A shop's items are not always split along a single option. *Scale 1:64 with the
overhead loadout* can be its own item on the books while *Scale 1:64* on its own
is another, and a mapping that could only name one option could not say so.

So the pair of columns becomes the same shape a variation already stores — a
list of {name, value} — matched as a subset of what the buyer chose. Existing
rows carry one option each and become one-element lists, which match exactly as
they did before.

Revision ID: 0019_option_item_sets
Revises: 0018_option_items
Create Date: 2026-08-15
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0019_option_item_sets'
down_revision = '0018_option_items'
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.add_column(
        'product_option_items',
        sa.Column('options', JSON_TYPE, nullable=False, server_default='[]'),
    )
    # One option each becomes a one-element list, matching exactly as before.
    op.execute(
        """
        update product_option_items
           set options = jsonb_build_array(
                   jsonb_build_object('name', option_name, 'value', option_value)
               )
        """
    )
    op.drop_constraint('uq_option_item_choice', 'product_option_items', type_='unique')
    op.drop_column('product_option_items', 'option_name')
    op.drop_column('product_option_items', 'option_value')


def downgrade() -> None:
    op.add_column(
        'product_option_items', sa.Column('option_name', sa.Text(), nullable=True)
    )
    op.add_column(
        'product_option_items', sa.Column('option_value', sa.Text(), nullable=True)
    )
    # Only the first option survives, because only one fits. A mapping that
    # pinned two would come back meaning something broader than it did, so it
    # is dropped rather than quietly widened onto somebody's invoices.
    op.execute(
        """
        update product_option_items
           set option_name = options -> 0 ->> 'name',
               option_value = options -> 0 ->> 'value'
        """
    )
    op.execute("delete from product_option_items where jsonb_array_length(options) <> 1")
    op.execute(
        "delete from product_option_items where option_name is null or option_value is null"
    )
    op.alter_column('product_option_items', 'option_name', nullable=False)
    op.alter_column('product_option_items', 'option_value', nullable=False)
    op.create_unique_constraint(
        'uq_option_item_choice',
        'product_option_items',
        ['product_id', 'option_name', 'option_value'],
    )
    op.drop_column('product_option_items', 'options')
