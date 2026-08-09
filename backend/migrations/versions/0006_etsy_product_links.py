"""match an Etsy listing to a product when it carries no SKU

Revision ID: 0006_etsy_product_links
Revises: 0005_etsy_options
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0006_etsy_product_links'
down_revision = '0005_etsy_options'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('order_lines', sa.Column('etsy_product_id', sa.BigInteger(), nullable=True))

    op.create_table(
        'etsy_product_links',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('product_id', sa.Uuid(), nullable=False),
        sa.Column('etsy_listing_id', sa.BigInteger(), nullable=False),
        sa.Column('etsy_product_id', sa.BigInteger(), nullable=True),
        sa.Column('listing_title', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_etsy_product_links_product_id', 'etsy_product_links', ['product_id'], unique=False
    )
    # One Etsy identity points at one product. Partial indexes, because a plain
    # unique over both columns would let a listing-wide link be added twice —
    # NULLs never collide.
    op.create_index(
        'uq_etsy_link_variant',
        'etsy_product_links',
        ['etsy_listing_id', 'etsy_product_id'],
        unique=True,
        postgresql_where=sa.text('etsy_product_id is not null'),
    )
    op.create_index(
        'uq_etsy_link_listing',
        'etsy_product_links',
        ['etsy_listing_id'],
        unique=True,
        postgresql_where=sa.text('etsy_product_id is null'),
    )


def downgrade() -> None:
    op.drop_index('uq_etsy_link_listing', table_name='etsy_product_links')
    op.drop_index('uq_etsy_link_variant', table_name='etsy_product_links')
    op.drop_index('ix_etsy_product_links_product_id', table_name='etsy_product_links')
    op.drop_table('etsy_product_links')
    op.drop_column('order_lines', 'etsy_product_id')
