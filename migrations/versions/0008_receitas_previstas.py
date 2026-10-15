"""receitas previstas

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-15 23:10:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('receita_prevista',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conta_id', sa.Integer(), nullable=False),
    sa.Column('descricao', sa.String(length=100), nullable=False),
    sa.Column('dia', sa.Integer(), nullable=False),
    sa.Column('valor', sa.BigInteger(), nullable=True),
    sa.Column('categoria_id', sa.Integer(), nullable=True),
    sa.Column('padrao', sa.String(length=200), nullable=True),
    sa.Column('ativa', sa.Boolean(), nullable=False),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['categoria_id'], ['categoria.id'], name=op.f('fk_receita_prevista_categoria_id_categoria'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['conta_id'], ['conta.id'], name=op.f('fk_receita_prevista_conta_id_conta'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_receita_prevista'))
    )
    with op.batch_alter_table('receita_prevista', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_receita_prevista_conta_id'), ['conta_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('receita_prevista', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_receita_prevista_conta_id'))
    op.drop_table('receita_prevista')
