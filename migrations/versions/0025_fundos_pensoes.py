"""fundos: posicoes (unidades, cotacao, aquisicao, risco), conta.data_contrato e conta.liquidez

Revision ID: 0025
Revises: 0024
Create Date: 2026-10-19 22:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0025'
down_revision = '0024'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('conta', schema=None) as batch_op:
        batch_op.add_column(sa.Column('data_contrato', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('liquidez', sa.BigInteger(), nullable=True))
    op.create_table('posicao',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conta_id', sa.Integer(), nullable=False),
    sa.Column('nome', sa.String(length=120), nullable=False),
    sa.Column('gestora', sa.String(length=80), nullable=True),
    sa.Column('categoria', sa.String(length=60), nullable=True),
    sa.Column('isin', sa.String(length=12), nullable=True),
    sa.Column('risco', sa.Integer(), nullable=True),
    sa.Column('unidades', sa.Float(), nullable=False),
    sa.Column('cotacao', sa.Float(), nullable=True),
    sa.Column('data_cotacao', sa.Date(), nullable=True),
    sa.Column('valor_aquisicao', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['conta_id'], ['conta.id'], name=op.f('fk_posicao_conta_id_conta'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_posicao'))
    )
    with op.batch_alter_table('posicao', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_posicao_conta_id'), ['conta_id'], unique=False)


def downgrade() -> None:
    op.drop_table('posicao')
    with op.batch_alter_table('conta', schema=None) as batch_op:
        batch_op.drop_column('liquidez')
        batch_op.drop_column('data_contrato')
