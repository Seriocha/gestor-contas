"""subscricoes e assinaturas

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-17 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('subscricao',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entidade_id', sa.Integer(), nullable=False),
    sa.Column('nome', sa.String(length=100), nullable=False),
    sa.Column('servico', sa.String(length=40), nullable=True),
    sa.Column('categoria_id', sa.Integer(), nullable=True),
    sa.Column('conta_id', sa.Integer(), nullable=True),
    sa.Column('valor', sa.BigInteger(), nullable=False),
    sa.Column('periodicidade', sa.String(length=12), nullable=False),
    sa.Column('dia_pagamento', sa.Integer(), nullable=True),
    sa.Column('data_inicio', sa.Date(), nullable=False),
    sa.Column('fim_fidelizacao', sa.Date(), nullable=True),
    sa.Column('data_fim', sa.Date(), nullable=True),
    sa.Column('padrao', sa.String(length=200), nullable=True),
    sa.Column('notas', sa.Text(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['categoria_id'], ['categoria.id'], name=op.f('fk_subscricao_categoria_id_categoria'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conta_id'], ['conta.id'], name=op.f('fk_subscricao_conta_id_conta'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], name=op.f('fk_subscricao_entidade_id_entidade'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_subscricao'))
    )
    with op.batch_alter_table('subscricao', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_subscricao_entidade_id'), ['entidade_id'], unique=False)
    op.create_table('subscricao_ignorada',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entidade_id', sa.Integer(), nullable=False),
    sa.Column('padrao', sa.String(length=200), nullable=False),
    sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], name=op.f('fk_subscricao_ignorada_entidade_id_entidade'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_subscricao_ignorada')),
    sa.UniqueConstraint('entidade_id', 'padrao', name=op.f('uq_subscricao_ignorada_entidade_id'))
    )


def downgrade() -> None:
    op.drop_table('subscricao_ignorada')
    op.drop_table('subscricao')
