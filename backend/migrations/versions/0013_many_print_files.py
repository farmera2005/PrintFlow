"""a product can be printed from more than one file

Revision ID: 0013_many_print_files
Revises: 0012_file_manager
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

JSON_TYPE = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

revision = '0013_many_print_files'
down_revision = '0012_file_manager'
branch_labels = None
depends_on = None


def _unique_constraints(table: str) -> list[str]:
    bind = op.get_bind()
    return [
        row[0]
        for row in bind.execute(
            sa.text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                WHERE rel.relname = :table AND con.contype = 'u'
                """
            ),
            {"table": table},
        )
    ]


def upgrade() -> None:
    # One file per product was the assumption; a farm whose library is sorted by
    # printer model has one file per model, all of them the same part. The
    # constraint's name depends on how the table was first created, so it is
    # looked up rather than guessed at.
    for name in _unique_constraints('print_mappings'):
        op.drop_constraint(name, 'print_mappings', type_='unique')
    op.create_index(
        'ix_print_mappings_product_id', 'print_mappings', ['product_id']
    )

    # Which file a plate uses is a question about which machine is free, and
    # that is not knowable when the plate is planned. The options travel with
    # the job and the choice is made at dispatch.
    op.add_column(
        'print_jobs',
        sa.Column('candidates', JSON_TYPE, nullable=False, server_default=sa.text("'[]'")),
    )
    # Plates already planned keep printing: their single file becomes their only
    # candidate, which is what they have always had.
    op.execute(
        """
        UPDATE print_jobs SET candidates = json_build_array(
            json_build_object(
                'archive_id', bambuddy_archive_id,
                'file_path', bambuddy_file_path,
                'name', NULL,
                'plate_number', COALESCE(plate_number, 1),
                'units_per_plate', units_expected,
                'printer_models', COALESCE(printer_models, '[]'::jsonb),
                'printer_id', printer_id,
                'print_options', '{}'::json
            )
        )::jsonb
        WHERE bambuddy_archive_id IS NOT NULL OR bambuddy_file_path IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column('print_jobs', 'candidates')
    # Going back to one file per product means choosing which one survives. The
    # oldest is the one that was there before this release.
    op.execute(
        """
        DELETE FROM print_mappings WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY product_id ORDER BY created_at, id
                ) AS rank FROM print_mappings
            ) ranked WHERE rank > 1
        )
        """
    )
    op.drop_index('ix_print_mappings_product_id', table_name='print_mappings')
    op.create_unique_constraint(
        'print_mappings_product_id_key', 'print_mappings', ['product_id']
    )
