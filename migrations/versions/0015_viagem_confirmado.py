"""movimentos de viagem ja ensinados a cada modelo

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-17 15:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('viagem_confirmado',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('viagem_id', sa.Integer(), nullable=False),
    sa.Column('movimento_id', sa.Integer(), nullable=False),
    sa.Column('modelo_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['modelo_id'], ['modelo_viagem.id'], name=op.f('fk_viagem_confirmado_modelo_id_modelo_viagem'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_viagem_confirmado_movimento_id_movimento'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['viagem_id'], ['viagem.id'], name=op.f('fk_viagem_confirmado_viagem_id_viagem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_viagem_confirmado')),
    sa.UniqueConstraint('viagem_id', 'movimento_id', 'modelo_id', name=op.f('uq_viagem_confirmado_viagem_id'))
    )
    with op.batch_alter_table('viagem_confirmado', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_viagem_confirmado_viagem_id'), ['viagem_id'], unique=False)


def downgrade() -> None:
    op.drop_table('viagem_confirmado')
