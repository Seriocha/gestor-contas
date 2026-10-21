"""saúde: perfis por pessoa, medidas diárias e treinos importados do Google Health (Takeout)

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-21 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0040'
down_revision = '0039'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'perfil_saude',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entidade_id', sa.Integer(), nullable=False),
        sa.Column('nome', sa.String(length=80), nullable=False),
        sa.Column('utilizador_id', sa.Integer(), nullable=True),
        sa.Column('altura_cm', sa.Float(), nullable=True),
        sa.Column('importado_em', sa.DateTime(), nullable=True),
        sa.Column('importado_ate', sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('entidade_id', 'nome'),
    )
    with op.batch_alter_table('perfil_saude', schema=None) as batch_op:
        batch_op.create_index('ix_perfil_saude_entidade_id', ['entidade_id'], unique=False)
    op.create_table(
        'medida_saude',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('perfil_id', sa.Integer(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('tipo', sa.String(length=30), nullable=False),
        sa.Column('valor', sa.Float(), nullable=False),
        sa.Column('fonte', sa.String(length=60), nullable=True),
        sa.ForeignKeyConstraint(['perfil_id'], ['perfil_saude.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('perfil_id', 'data', 'tipo'),
    )
    with op.batch_alter_table('medida_saude', schema=None) as batch_op:
        batch_op.create_index('ix_medida_saude_perfil_id', ['perfil_id'], unique=False)
        batch_op.create_index('ix_medida_saude_data', ['data'], unique=False)
    op.create_table(
        'treino_saude',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('perfil_id', sa.Integer(), nullable=False),
        sa.Column('externo_id', sa.String(length=40), nullable=False),
        sa.Column('inicio', sa.DateTime(), nullable=False),
        sa.Column('duracao_min', sa.Float(), nullable=True),
        sa.Column('atividade', sa.String(length=60), nullable=False),
        sa.Column('calorias', sa.Float(), nullable=True),
        sa.Column('passos', sa.Integer(), nullable=True),
        sa.Column('distancia_km', sa.Float(), nullable=True),
        sa.Column('fc_media', sa.Float(), nullable=True),
        sa.Column('fc_maxima', sa.Float(), nullable=True),
        sa.Column('manual', sa.Boolean(), server_default='0', nullable=False),
        sa.ForeignKeyConstraint(['perfil_id'], ['perfil_saude.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('perfil_id', 'externo_id'),
    )
    with op.batch_alter_table('treino_saude', schema=None) as batch_op:
        batch_op.create_index('ix_treino_saude_perfil_id', ['perfil_id'], unique=False)
    # o módulo fica ligado nas entidades pessoais (as heranças não têm saúde)
    op.execute("UPDATE entidade SET modulos = modulos || ',saude' "
               "WHERE tipo = 'pessoal' AND modulos IS NOT NULL AND modulos != '' AND instr(modulos, 'saude') = 0")


def downgrade() -> None:
    op.execute("UPDATE entidade SET modulos = replace(replace(modulos, ',saude', ''), 'saude', '')")
    op.drop_table('treino_saude')
    op.drop_table('medida_saude')
    op.drop_table('perfil_saude')
