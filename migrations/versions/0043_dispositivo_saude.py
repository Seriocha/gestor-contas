"""telemóveis emparelhados que enviam os dados do Health Connect

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-25 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'dispositivo_saude',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('perfil_id', sa.Integer(), nullable=False),
        sa.Column('utilizador_id', sa.Integer(), nullable=False),
        sa.Column('nome', sa.String(length=60), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('versao_sessao', sa.Integer(), nullable=False),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('ultimo_envio', sa.DateTime(), nullable=True),
        sa.Column('ultimo_resumo', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['perfil_id'], ['perfil_saude.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_dispositivo_saude_perfil_id', 'dispositivo_saude', ['perfil_id'])


def downgrade() -> None:
    op.drop_index('ix_dispositivo_saude_perfil_id', table_name='dispositivo_saude')
    op.drop_table('dispositivo_saude')
