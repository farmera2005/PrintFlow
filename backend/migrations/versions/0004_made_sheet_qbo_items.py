"""made-sheet lines can name a QuickBooks item directly

Revision ID: 0004_made_sheet_qbo_items
Revises: 0003_made_sheets
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0004_made_sheet_qbo_items'
down_revision = '0003_made_sheets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('made_sheet_lines', sa.Column('qbo_item_id', sa.Text(), nullable=True))
    op.add_column('made_sheet_lines', sa.Column('qbo_item_name', sa.Text(), nullable=True))
    op.alter_column('made_sheet_lines', 'product_id', existing_type=sa.Uuid(), nullable=True)

    # A plain unique over both columns would not stop the same thing being
    # listed twice — once by product, once by item — because NULLs never
    # collide. Two partial indexes each police their own column.
    op.drop_constraint('uq_made_sheet_line_product', 'made_sheet_lines', type_='unique')
    op.create_index(
        'uq_made_sheet_line_product',
        'made_sheet_lines',
        ['sheet_id', 'product_id'],
        unique=True,
        postgresql_where=sa.text('product_id is not null'),
    )
    op.create_index(
        'uq_made_sheet_line_qbo_item',
        'made_sheet_lines',
        ['sheet_id', 'qbo_item_id'],
        unique=True,
        postgresql_where=sa.text('qbo_item_id is not null'),
    )
    op.create_check_constraint(
        'ck_made_sheet_line_one_target',
        'made_sheet_lines',
        '(product_id is not null) <> (qbo_item_id is not null)',
    )


def downgrade() -> None:
    op.drop_constraint('ck_made_sheet_line_one_target', 'made_sheet_lines', type_='check')
    op.drop_index('uq_made_sheet_line_qbo_item', table_name='made_sheet_lines')
    op.drop_index('uq_made_sheet_line_product', table_name='made_sheet_lines')
    # Lines that name only a QuickBooks item have no product to fall back to.
    op.execute('delete from made_sheet_lines where product_id is null')
    op.alter_column('made_sheet_lines', 'product_id', existing_type=sa.Uuid(), nullable=False)
    op.create_unique_constraint(
        'uq_made_sheet_line_product', 'made_sheet_lines', ['sheet_id', 'product_id']
    )
    op.drop_column('made_sheet_lines', 'qbo_item_name')
    op.drop_column('made_sheet_lines', 'qbo_item_id')
