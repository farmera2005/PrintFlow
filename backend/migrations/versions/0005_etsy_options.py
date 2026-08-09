"""etsy options on order lines, and rules that change a BOM

Revision ID: 0005_etsy_options
Revises: 0004_made_sheet_qbo_items
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0005_etsy_options'
down_revision = '0004_made_sheet_qbo_items'
branch_labels = None
depends_on = None

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.add_column(
        'order_lines',
        sa.Column('variations', JSON_TYPE, nullable=False, server_default='[]'),
    )
    op.add_column(
        'order_lines',
        sa.Column('option_effects', JSON_TYPE, nullable=False, server_default='[]'),
    )

    op.create_table(
        'bom_option_rules',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('bundle_id', sa.Uuid(), nullable=False),
        sa.Column('option_name', sa.Text(), nullable=False),
        sa.Column('option_value', sa.Text(), nullable=False),
        sa.Column('replaces_id', sa.Uuid(), nullable=True),
        sa.Column('component_id', sa.Uuid(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('quantity is null or quantity > 0', name='ck_bom_option_quantity_positive'),
        sa.CheckConstraint(
            'replaces_id is null or replaces_id <> component_id',
            name='ck_bom_option_no_self_swap',
        ),
        sa.ForeignKeyConstraint(['bundle_id'], ['products.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['component_id'], ['products.id'], ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['replaces_id'], ['products.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_bom_option_rules_bundle_id', 'bom_option_rules', ['bundle_id'], unique=False
    )
    # Lower-cased, because the option strings are typed by hand in Etsy's
    # listing editor and "Red" and "red" are the same choice.
    op.create_index(
        'uq_bom_option_rule',
        'bom_option_rules',
        ['bundle_id', sa.text('lower(option_name)'), sa.text('lower(option_value)'), 'component_id'],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index('uq_bom_option_rule', table_name='bom_option_rules')
    op.drop_index('ix_bom_option_rules_bundle_id', table_name='bom_option_rules')
    op.drop_table('bom_option_rules')
    op.drop_column('order_lines', 'option_effects')
    op.drop_column('order_lines', 'variations')
