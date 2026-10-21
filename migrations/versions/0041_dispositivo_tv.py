"""televisões emparelhadas (vista /tv só de consulta)

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-22 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0041'
down_revision = '0040'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'dispositivo_tv',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('utilizador_id', sa.Integer(), nullable=False),
        sa.Column('nome', sa.String(length=60), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('entidades', sa.String(length=200), nullable=False),
        sa.Column('versao_sessao', sa.Integer(), nullable=False),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('ultimo_acesso', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_dispositivo_tv_utilizador_id', 'dispositivo_tv', ['utilizador_id'])


def downgrade() -> None:
    op.drop_index('ix_dispositivo_tv_utilizador_id', table_name='dispositivo_tv')
    op.drop_table('dispositivo_tv')
