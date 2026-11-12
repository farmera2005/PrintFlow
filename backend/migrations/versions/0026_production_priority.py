"""things the shop has decided go first

The To Make list sorts by how long an order has waited, which is the right
default and the wrong answer perhaps twice a week: a rush job, a customer on
the phone, a machine that is free now and only takes this one part. Until now
the only way to say so was to remember it.

A list rather than a flag, because "first" is a position and two things can
both be urgent. Keyed on the thing — product and variation — because that is
what the To Make list is keyed on, and because a priority tied to an order
line would vanish the moment the line was made, quietly un-prioritising
something nobody said they had finished with.

Revision ID: 0026_production_priority
Revises: 0025_order_files
Create Date: 2026-11-12
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0026_production_priority'
down_revision = '0025_order_files'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'production_priorities',
        sa.Column('id', sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            'product_id', sa.Uuid(as_uuid=True),
            sa.ForeignKey('products.id', ondelete='CASCADE'), nullable=False,
        ),
        # Null means the product unvaried, not the product in general.
        sa.Column(
            'variation_id', sa.Uuid(as_uuid=True),
            sa.ForeignKey('product_variations.id', ondelete='CASCADE'),
            nullable=True,
        ),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pinned_by', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint('position >= 0', name='ck_production_priority_position'),
    )
    op.create_index(
        'ix_production_priorities_product', 'production_priorities', ['product_id']
    )
    op.create_index(
        'ix_production_priorities_position', 'production_priorities', ['position']
    )
    # Partial indexes, because a plain unique over both columns would let the
    # unvaried product be pinned twice: NULLs never collide.
    op.create_index(
        'uq_production_priority_product',
        'production_priorities',
        ['product_id'],
        unique=True,
        postgresql_where=sa.text('variation_id is null'),
        sqlite_where=sa.text('variation_id is null'),
    )
    op.create_index(
        'uq_production_priority_variation',
        'production_priorities',
        ['product_id', 'variation_id'],
        unique=True,
        postgresql_where=sa.text('variation_id is not null'),
        sqlite_where=sa.text('variation_id is not null'),
    )


def downgrade() -> None:
    op.drop_index(
        'uq_production_priority_variation', table_name='production_priorities'
    )
    op.drop_index('uq_production_priority_product', table_name='production_priorities')
    op.drop_index(
        'ix_production_priorities_position', table_name='production_priorities'
    )
    op.drop_index(
        'ix_production_priorities_product', table_name='production_priorities'
    )
    op.drop_table('production_priorities')
