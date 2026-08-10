"""printer models rather than one preferred printer

Revision ID: 0011_printer_models
Revises: 0010_manual_order_status
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

revision = '0011_printer_models'
down_revision = '0010_manual_order_status'
branch_labels = None
depends_on = None


def _models_column() -> sa.Column:
    return sa.Column(
        'printer_models', JSON_TYPE, nullable=False, server_default=sa.text("'[]'")
    )


def upgrade() -> None:
    for table in ('print_mappings', 'product_variations', 'print_jobs'):
        op.add_column(table, _models_column())

    # A preferred printer named one machine; a model names everything capable of
    # the job. There is no way to translate one into the other here — which
    # model a printer id is belongs to Bambuddy, not this database — so the old
    # column goes and the new one starts empty, meaning "any printer".
    op.drop_column('print_mappings', 'preferred_printer_id')
    op.drop_column('product_variations', 'preferred_printer_id')


def downgrade() -> None:
    op.add_column('print_mappings', sa.Column('preferred_printer_id', sa.Integer(), nullable=True))
    op.add_column(
        'product_variations', sa.Column('preferred_printer_id', sa.Integer(), nullable=True)
    )
    for table in ('print_jobs', 'product_variations', 'print_mappings'):
        op.drop_column(table, 'printer_models')
