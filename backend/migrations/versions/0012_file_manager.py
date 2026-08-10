"""print files picked out of Bambuddy's file manager

Revision ID: 0012_file_manager
Revises: 0011_printer_models
Create Date: 2026-08-10
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0012_file_manager'
down_revision = '0011_printer_models'
branch_labels = None
depends_on = None

SOURCED = ('print_mappings', 'product_variations', 'print_jobs')


def upgrade() -> None:
    for table in SOURCED:
        op.add_column(table, sa.Column('bambuddy_file_path', sa.Text(), nullable=True))
    # Where the file was picked from, which is also where the plate has to go.
    # Print jobs already have printer_id for this.
    for table in ('print_mappings', 'product_variations'):
        op.add_column(table, sa.Column('bambuddy_printer_id', sa.Integer(), nullable=True))

    # A file manager entry can have a path and no id at all, so the id stops
    # being required. One of the two still has to be there, or the mapping says
    # nothing about what to print.
    op.alter_column('print_mappings', 'bambuddy_archive_id', existing_type=sa.Integer(),
                    nullable=True)
    op.alter_column('print_jobs', 'bambuddy_archive_id', existing_type=sa.Integer(),
                    nullable=True)
    op.create_check_constraint(
        'ck_mapping_has_a_source',
        'print_mappings',
        'bambuddy_archive_id IS NOT NULL OR bambuddy_file_path IS NOT NULL',
    )


def downgrade() -> None:
    op.drop_constraint('ck_mapping_has_a_source', 'print_mappings', type_='check')
    # Rows that only ever had a path cannot go back to an id-only world; they
    # are the ones this release made possible, so they go rather than take the
    # whole downgrade with them.
    op.execute('DELETE FROM print_jobs WHERE bambuddy_archive_id IS NULL')
    op.execute('DELETE FROM print_mappings WHERE bambuddy_archive_id IS NULL')
    op.alter_column('print_jobs', 'bambuddy_archive_id', existing_type=sa.Integer(),
                    nullable=False)
    op.alter_column('print_mappings', 'bambuddy_archive_id', existing_type=sa.Integer(),
                    nullable=False)

    for table in ('print_mappings', 'product_variations'):
        op.drop_column(table, 'bambuddy_printer_id')
    for table in SOURCED:
        op.drop_column(table, 'bambuddy_file_path')
