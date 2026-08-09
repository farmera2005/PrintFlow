"""variants as products of their own, nested under a master

Revision ID: 0008_variant_products
Revises: 0007_product_variations
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0008_variant_products'
down_revision = '0007_product_variations'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('products', sa.Column('parent_id', sa.Uuid(), nullable=True))
    op.create_index('ix_products_parent_id', 'products', ['parent_id'], unique=False)
    op.create_check_constraint(
        'ck_products_parent_not_self', 'products', 'parent_id is null or parent_id <> id'
    )
    # RESTRICT: a master with variants still attached should say so rather than
    # take them with it, or orphan them silently.
    op.create_foreign_key(
        'products_parent_id_fkey', 'products', 'products', ['parent_id'], ['id'],
        ondelete='RESTRICT',
    )

    op.add_column(
        'product_variations', sa.Column('variant_product_id', sa.Uuid(), nullable=True)
    )
    op.create_index(
        'ix_product_variations_variant_product_id',
        'product_variations',
        ['variant_product_id'],
        unique=False,
    )
    op.create_foreign_key(
        'product_variations_variant_product_id_fkey',
        'product_variations',
        'products',
        ['variant_product_id'],
        ['id'],
        ondelete='RESTRICT',
    )


def downgrade() -> None:
    op.drop_constraint(
        'product_variations_variant_product_id_fkey', 'product_variations', type_='foreignkey'
    )
    op.drop_index(
        'ix_product_variations_variant_product_id', table_name='product_variations'
    )
    op.drop_column('product_variations', 'variant_product_id')

    op.drop_constraint('products_parent_id_fkey', 'products', type_='foreignkey')
    op.drop_constraint('ck_products_parent_not_self', 'products', type_='check')
    op.drop_index('ix_products_parent_id', table_name='products')
    op.drop_column('products', 'parent_id')
