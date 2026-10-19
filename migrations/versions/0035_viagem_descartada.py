"""períodos de viagem detetados que o utilizador descartou

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-20 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0035'
down_revision = '0034'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'viagem_descartada',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entidade_id', sa.Integer(), nullable=False),
        sa.Column('data_ini', sa.Date(), nullable=False),
        sa.Column('data_fim', sa.Date(), nullable=False),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('viagem_descartada', schema=None) as batch_op:
        batch_op.create_index('ix_viagem_descartada_entidade_id', ['entidade_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('viagem_descartada', schema=None) as batch_op:
        batch_op.drop_index('ix_viagem_descartada_entidade_id')
    op.drop_table('viagem_descartada')
