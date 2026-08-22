"""machines and their maintenance logs

PrintFlow's own record of the printers, kept apart from Bambuddy on purpose. A
maintenance history outlives the software watching the machine: shops change
farm managers, re-add printers under new ids, run machines Bambuddy never saw,
and keep servicing one long after it has left the farm. A log keyed on somebody
else's id would lose all of that the day that id changed.

So `bambuddy_printer_id` is a nullable convenience — it lets the tab show what a
machine is doing now — and nothing depends on it.

Revision ID: 0022_maintenance
Revises: 0021_order_expenses
Create Date: 2026-08-16
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0022_maintenance'
down_revision = '0021_order_expenses'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'machines',
        sa.Column('id', sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('model', sa.Text(), nullable=True),
        sa.Column('serial', sa.Text(), nullable=True),
        sa.Column('bambuddy_printer_id', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.UniqueConstraint('name', name='uq_machines_name'),
    )
    op.create_index('ix_machines_bambuddy', 'machines', ['bambuddy_printer_id'])

    op.create_table(
        'maintenance_logs',
        sa.Column('id', sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            'machine_id', sa.Uuid(as_uuid=True),
            sa.ForeignKey('machines.id', ondelete='CASCADE'), nullable=False,
        ),
        sa.Column('logged_on', sa.Date(), nullable=False),
        sa.Column('hours', sa.Numeric(12, 2), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='serviced'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('actor', sa.Text(), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('serviced','ok','due','attention','down')",
            name='ck_maintenance_logs_status',
        ),
    )
    op.create_index('ix_maintenance_logs_machine_id', 'maintenance_logs', ['machine_id'])
    op.create_index(
        'ix_maintenance_logs_machine_date', 'maintenance_logs',
        ['machine_id', 'logged_on'],
    )


def downgrade() -> None:
    op.drop_table('maintenance_logs')
    op.drop_index('ix_machines_bambuddy', table_name='machines')
    op.drop_table('machines')
