"""product variations, and the variation an order line resolved to

Revision ID: 0007_product_variations
Revises: 0006_etsy_product_links
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# Same variant JSON the other migrations use, so a non-Postgres target still builds.
JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

revision = '0007_product_variations'
down_revision = '0006_etsy_product_links'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'product_variations',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('product_id', sa.Uuid(), nullable=False),
        sa.Column('etsy_listing_id', sa.BigInteger(), nullable=True),
        sa.Column('etsy_product_id', sa.BigInteger(), nullable=True),
        sa.Column(
            'options',
            JSON_TYPE,
            nullable=False,
        ),
        sa.Column('label', sa.Text(), nullable=False),
        sa.Column('bambuddy_archive_id', sa.Integer(), nullable=True),
        sa.Column('bambuddy_archive_name', sa.Text(), nullable=True),
        sa.Column('plate_number', sa.Integer(), nullable=True),
        sa.Column('units_per_plate', sa.Integer(), nullable=True),
        sa.Column('preferred_printer_id', sa.Integer(), nullable=True),
        sa.Column('qbo_item_id', sa.Text(), nullable=True),
        sa.Column('qbo_item_name', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint(
            'units_per_plate is null or units_per_plate > 0',
            name='ck_variation_units_per_plate_positive',
        ),
        sa.CheckConstraint(
            'plate_number is null or plate_number > 0',
            name='ck_variation_plate_positive',
        ),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_product_variations_product_id', 'product_variations', ['product_id'], unique=False
    )
    # One row per Etsy variation. Partial, because a variation described only by
    # its option values carries no id and several of those must coexist.
    op.create_index(
        'uq_variation_etsy_product',
        'product_variations',
        ['etsy_product_id'],
        unique=True,
        postgresql_where=sa.text('etsy_product_id is not null'),
    )

    op.add_column('order_lines', sa.Column('variation_id', sa.Uuid(), nullable=True))
    op.create_index(
        'ix_order_lines_variation_id', 'order_lines', ['variation_id'], unique=False
    )
    # SET NULL rather than RESTRICT: deleting a variation is a catalogue edit and
    # must not be blocked by orders that once bought it. The line keeps the
    # options the buyer actually chose either way.
    op.create_foreign_key(
        'order_lines_variation_id_fkey',
        'order_lines',
        'product_variations',
        ['variation_id'],
        ['id'],
        ondelete='SET NULL',
    )


def downgrade() -> None:
    op.drop_constraint('order_lines_variation_id_fkey', 'order_lines', type_='foreignkey')
    op.drop_index('ix_order_lines_variation_id', table_name='order_lines')
    op.drop_column('order_lines', 'variation_id')
    op.drop_index('uq_variation_etsy_product', table_name='product_variations')
    op.drop_index('ix_product_variations_product_id', table_name='product_variations')
    op.drop_table('product_variations')
