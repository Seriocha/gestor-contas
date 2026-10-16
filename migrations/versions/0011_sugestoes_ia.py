"""cache das sugestoes da IA para descricoes sem categoria

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('sugestao_ia',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('chave', sa.String(length=200), nullable=False),
    sa.Column('comerciante', sa.String(length=120), nullable=True),
    sa.Column('atividade', sa.String(length=200), nullable=True),
    sa.Column('categoria_id', sa.Integer(), nullable=True),
    sa.Column('confianca', sa.Float(), nullable=True),
    sa.Column('modelo', sa.String(length=60), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['categoria_id'], ['categoria.id'], name=op.f('fk_sugestao_ia_categoria_id_categoria'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sugestao_ia')),
    sa.UniqueConstraint('chave', name=op.f('uq_sugestao_ia_chave'))
    )


def downgrade() -> None:
    op.drop_table('sugestao_ia')
