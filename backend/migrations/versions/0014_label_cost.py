"""what the shipping label cost

Labels are the one thing PrintFlow spends money on, and until now the price
disappeared into ShipStation the moment the label was bought. It is stored on
the order so the board can show it — and so a question about last month's
postage can be answered from the orders themselves.

Revision ID: 0014_label_cost
Revises: 0013_many_print_files
Create Date: 2026-08-11
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '0014_label_cost'
down_revision = '0013_many_print_files'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no default: an order whose label was bought before this
    # release has no cost to show, which is different from one that cost zero.
    op.add_column('orders', sa.Column('label_cost', sa.Numeric(12, 4), nullable=True))
    op.add_column('orders', sa.Column('label_currency', sa.Text(), nullable=True))
    op.create_check_constraint(
        'ck_orders_label_cost_not_negative', 'orders', 'label_cost >= 0'
    )


def downgrade() -> None:
    op.drop_constraint('ck_orders_label_cost_not_negative', 'orders', type_='check')
    op.drop_column('orders', 'label_currency')
    op.drop_column('orders', 'label_cost')
