"""via verde: linhas do extrato e matricula do carro

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-18 14:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('matricula', sa.String(length=12), nullable=True))
    op.create_table('via_verde',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entidade_id', sa.Integer(), nullable=False),
    sa.Column('contrato', sa.String(length=20), nullable=True),
    sa.Column('matricula', sa.String(length=12), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('hora', sa.String(length=5), nullable=True),
    sa.Column('entrada', sa.String(length=80), nullable=True),
    sa.Column('saida', sa.String(length=80), nullable=True),
    sa.Column('valor', sa.BigInteger(), nullable=False),
    sa.Column('operador', sa.String(length=80), nullable=True),
    sa.Column('servico', sa.String(length=60), nullable=True),
    sa.Column('documento', sa.String(length=40), nullable=True),
    sa.Column('data_pagamento', sa.Date(), nullable=True),
    sa.Column('hash', sa.String(length=40), nullable=False),
    sa.Column('movimento_id', sa.Integer(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], name=op.f('fk_via_verde_entidade_id_entidade'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_via_verde_movimento_id_movimento'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_via_verde')),
    sa.UniqueConstraint('entidade_id', 'hash', name=op.f('uq_via_verde_entidade_id'))
    )
    with op.batch_alter_table('via_verde', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_via_verde_entidade_id'), ['entidade_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_via_verde_data'), ['data'], unique=False)
        batch_op.create_index(batch_op.f('ix_via_verde_movimento_id'), ['movimento_id'], unique=False)


def downgrade() -> None:
    op.drop_table('via_verde')
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.drop_column('matricula')
