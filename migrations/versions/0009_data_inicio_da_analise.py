"""data de inicio da analise por entidade

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-16 09:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('entidade', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data_inicio', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('entidade', schema=None) as batch_op:
        batch_op.drop_column('data_inicio')
