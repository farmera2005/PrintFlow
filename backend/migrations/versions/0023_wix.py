"""orders can come from more than one shop window

PrintFlow's order table was Etsy's: an Etsy receipt id, not null and unique, was
what an order *was*. A second sales channel needs a way to say which door an
order came through, and its own id in whatever shape that channel uses — Etsy
numbers receipts, Wix uses UUIDs.

So `source` and `external_id` become the general pair, `etsy_receipt_id` becomes
nullable and keeps its uniqueness as a partial index, and every existing row is
backfilled as Etsy with its receipt id as the external one. Nothing downstream
changes: printing, assembly, labels and the books treat an order the same
whichever channel sold it.

Revision ID: 0023_wix
Revises: 0022_maintenance
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0023_wix'
down_revision = '0022_maintenance'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'orders',
        sa.Column('source', sa.Text(), nullable=False, server_default='etsy'),
    )
    op.add_column('orders', sa.Column('external_id', sa.Text(), nullable=True))
    # Every order there has ever been came from Etsy, so that is what they are.
    op.execute("update orders set source = 'etsy', external_id = etsy_receipt_id::text")

    # The receipt id stops being what an order *is* and becomes what an Etsy
    # order has. Its uniqueness survives as a partial index, so two Wix orders —
    # which have no receipt id at all — do not collide on null.
    op.alter_column('orders', 'etsy_receipt_id', existing_type=sa.BigInteger(), nullable=True)
    op.drop_constraint('orders_etsy_receipt_id_key', 'orders', type_='unique')
    op.create_index(
        'uq_orders_etsy_receipt', 'orders', ['etsy_receipt_id'],
        unique=True, postgresql_where=sa.text('etsy_receipt_id is not null'),
    )
    op.create_index(
        'uq_orders_source_external', 'orders', ['source', 'external_id'],
        unique=True, postgresql_where=sa.text('external_id is not null'),
    )

    for column in ('wix_catalog_item_id', 'wix_variant_id', 'wix_line_item_id'):
        op.add_column('order_lines', sa.Column(column, sa.Text(), nullable=True))

    op.create_table(
        'wix_product_links',
        sa.Column('id', sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            'product_id', sa.Uuid(as_uuid=True),
            sa.ForeignKey('products.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('wix_catalog_item_id', sa.Text(), nullable=False),
        sa.Column('wix_variant_id', sa.Text(), nullable=True),
        sa.Column('item_title', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
    )
    op.create_index('ix_wix_product_links_product_id', 'wix_product_links', ['product_id'])
    op.create_index(
        'uq_wix_link_variant', 'wix_product_links',
        ['wix_catalog_item_id', 'wix_variant_id'], unique=True,
        postgresql_where=sa.text('wix_variant_id is not null'),
    )
    op.create_index(
        'uq_wix_link_item', 'wix_product_links', ['wix_catalog_item_id'], unique=True,
        postgresql_where=sa.text('wix_variant_id is null'),
    )


def downgrade() -> None:
    op.drop_table('wix_product_links')
    for column in ('wix_line_item_id', 'wix_variant_id', 'wix_catalog_item_id'):
        op.drop_column('order_lines', column)
    op.drop_index('uq_orders_source_external', table_name='orders')
    op.drop_index('uq_orders_etsy_receipt', table_name='orders')
    # Anything that did not come from Etsy has no receipt id to go back to, and
    # the column cannot be null again — so those orders cannot survive this.
    op.execute("delete from orders where etsy_receipt_id is null")
    op.alter_column('orders', 'etsy_receipt_id', existing_type=sa.BigInteger(), nullable=False)
    op.create_unique_constraint('orders_etsy_receipt_id_key', 'orders', ['etsy_receipt_id'])
    op.drop_column('orders', 'external_id')
    op.drop_column('orders', 'source')
