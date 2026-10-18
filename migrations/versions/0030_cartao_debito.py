"""cartões de débito e as compras de cada cartão (ligadas aos débitos da conta)

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-18 23:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0030'
down_revision = '0029'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'cartao_debito',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conta_id', sa.Integer(), nullable=False),
        sa.Column('final', sa.String(length=4), nullable=False),
        sa.Column('nome', sa.String(length=60), nullable=False),
        sa.Column('cor', sa.String(length=7), nullable=True),
        sa.ForeignKeyConstraint(['conta_id'], ['conta.id'], name=op.f('fk_cartao_debito_conta_id_conta'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_cartao_debito')),
        sa.UniqueConstraint('conta_id', 'final', name=op.f('uq_cartao_debito_conta_id')),
    )
    with op.batch_alter_table('cartao_debito', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_cartao_debito_conta_id'), ['conta_id'], unique=False)
    op.create_table(
        'movimento_cartao',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('cartao_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('montante', sa.BigInteger(), nullable=False),
        sa.Column('descricao', sa.String(length=200), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('movimento_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['cartao_id'], ['cartao_debito.id'], name=op.f('fk_movimento_cartao_cartao_id_cartao_debito'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_movimento_cartao_movimento_id_movimento'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_movimento_cartao')),
        sa.UniqueConstraint('cartao_id', 'hash', name=op.f('uq_movimento_cartao_cartao_id')),
    )
    with op.batch_alter_table('movimento_cartao', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_movimento_cartao_cartao_id'), ['cartao_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_movimento_cartao_movimento_id'), ['movimento_id'], unique=False)


def downgrade() -> None:
    op.drop_table('movimento_cartao')
    op.drop_table('cartao_debito')
