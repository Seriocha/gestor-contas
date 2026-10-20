"""plafond de manutenção dos carros (viatura da empresa): valor, anos e data de início

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-20 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plafond_manutencao', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('plafond_anos', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('plafond_inicio', sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.drop_column('plafond_inicio')
        batch_op.drop_column('plafond_anos')
        batch_op.drop_column('plafond_manutencao')
