"""made sheets

Revision ID: 0003_made_sheets
Revises: 0002_tls_certificates
Create Date: 2026-08-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003_made_sheets'
down_revision = '0002_tls_certificates'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'made_sheets',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('reference', sa.Text(), nullable=False),
        sa.Column('made_on', sa.DateTime(timezone=True), nullable=False),
        sa.Column('memo', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('qbo_purchase_id', sa.Text(), nullable=True),
        sa.Column('qbo_doc_number', sa.Text(), nullable=True),
        sa.Column('qbo_sync_token', sa.Text(), nullable=True),
        sa.Column(
            'qbo_request',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=True,
        ),
        sa.Column(
            'qbo_response',
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
            nullable=True,
        ),
        sa.Column('idempotency_key', sa.Uuid(), nullable=False),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('posted_by', sa.Text(), nullable=True),
        sa.Column('voided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('voided_by', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint("status in ('draft','posted','voided')", name='ck_made_sheets_status'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_made_sheets_status', 'made_sheets', ['status'], unique=False)

    op.create_table(
        'made_sheet_lines',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('sheet_id', sa.Uuid(), nullable=False),
        sa.Column('product_id', sa.Uuid(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_cost', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('cost_from_bom', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.CheckConstraint('quantity > 0', name='ck_made_sheet_line_quantity_positive'),
        sa.CheckConstraint('unit_cost >= 0', name='ck_made_sheet_line_cost_not_negative'),
        sa.ForeignKeyConstraint(['sheet_id'], ['made_sheets.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['product_id'], ['products.id'], ondelete='RESTRICT'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sheet_id', 'product_id', name='uq_made_sheet_line_product'),
    )
    op.create_index(
        'ix_made_sheet_lines_sheet_id', 'made_sheet_lines', ['sheet_id'], unique=False
    )


def downgrade() -> None:
    op.drop_index('ix_made_sheet_lines_sheet_id', table_name='made_sheet_lines')
    op.drop_table('made_sheet_lines')
    op.drop_index('ix_made_sheets_status', table_name='made_sheets')
    op.drop_table('made_sheets')
