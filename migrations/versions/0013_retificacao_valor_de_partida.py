"""retificacao de saldo marcada como valor de partida (capital, nao ganho)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-17 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('retificacao_saldo', schema=None) as batch_op:
        batch_op.add_column(sa.Column('partida', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('retificacao_saldo', schema=None) as batch_op:
        batch_op.drop_column('partida')
