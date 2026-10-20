"""contadores de eletricidade (CPE) e leituras diárias importadas da E-Redes

Revision ID: 0039
Revises: 0038
Create Date: 2026-10-20 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0039'
down_revision = '0038'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'contador_energia',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entidade_id', sa.Integer(), nullable=False),
        sa.Column('cpe', sa.String(length=30), nullable=False),
        sa.Column('bem_id', sa.Integer(), nullable=True),
        sa.Column('morada', sa.String(length=200), nullable=True),
        sa.Column('numero', sa.String(length=30), nullable=True),
        sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entidade_id', 'cpe'),
    )
    with op.batch_alter_table('contador_energia', schema=None) as batch_op:
        batch_op.create_index('ix_contador_energia_entidade_id', ['entidade_id'], unique=False)
    op.create_table(
        'leitura_energia',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('contador_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('valores', sa.Text(), nullable=False),
        sa.Column('tipo', sa.String(length=20), nullable=True),
        sa.ForeignKeyConstraint(['contador_id'], ['contador_energia.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('contador_id', 'data'),
    )
    with op.batch_alter_table('leitura_energia', schema=None) as batch_op:
        batch_op.create_index('ix_leitura_energia_contador_id', ['contador_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('leitura_energia', schema=None) as batch_op:
        batch_op.drop_index('ix_leitura_energia_contador_id')
    op.drop_table('leitura_energia')
    with op.batch_alter_table('contador_energia', schema=None) as batch_op:
        batch_op.drop_index('ix_contador_energia_entidade_id')
    op.drop_table('contador_energia')
