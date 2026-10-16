"""sugestoes de pares de movimentos ignoradas

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16 22:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('par_ignorado',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('debito_id', sa.Integer(), nullable=False),
    sa.Column('credito_id', sa.Integer(), nullable=False),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['credito_id'], ['movimento.id'], name=op.f('fk_par_ignorado_credito_id_movimento'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['debito_id'], ['movimento.id'], name=op.f('fk_par_ignorado_debito_id_movimento'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_par_ignorado')),
    sa.UniqueConstraint('debito_id', 'credito_id', name=op.f('uq_par_ignorado_debito_id'))
    )


def downgrade() -> None:
    op.drop_table('par_ignorado')
