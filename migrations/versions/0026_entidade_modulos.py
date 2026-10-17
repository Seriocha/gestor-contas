"""entidade: modulos usados e 2.o cenario da quota (heranca)

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-20 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0026'
down_revision = '0025'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('entidade', schema=None) as batch_op:
        batch_op.add_column(sa.Column('quota_futura', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('quota_futura_nota', sa.String(length=100), nullable=True))
        batch_op.add_column(sa.Column('modulos', sa.String(length=120), nullable=False,
                                      server_default='imoveis,viaturas,subscricoes,viagens'))


def downgrade() -> None:
    with op.batch_alter_table('entidade', schema=None) as batch_op:
        batch_op.drop_column('modulos')
        batch_op.drop_column('quota_futura_nota')
        batch_op.drop_column('quota_futura')
