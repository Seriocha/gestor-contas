"""motor de classificação dos documentos do Paperless: regras versionadas, classificações e auditoria

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-23 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0042'
down_revision = '0041'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'regra_documento',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('nome', sa.String(length=120), nullable=False),
        sa.Column('ativa', sa.Boolean(), nullable=False),
        sa.Column('ordem', sa.Integer(), nullable=False),
        sa.Column('algoritmo', sa.String(length=10), nullable=False),
        sa.Column('texto', sa.String(length=500), nullable=False),
        sa.Column('so_tipo', sa.String(length=128), nullable=True),
        sa.Column('so_sem_correspondente', sa.Boolean(), nullable=False),
        sa.Column('correspondente', sa.String(length=128), nullable=True),
        sa.Column('tipo', sa.String(length=128), nullable=True),
        sa.Column('etiquetas', sa.String(length=500), nullable=False),
        sa.Column('extrair', sa.String(length=200), nullable=False),
        sa.Column('confianca', sa.String(length=10), nullable=False),
        sa.Column('nota', sa.Text(), nullable=True),
        sa.Column('origem', sa.String(length=10), nullable=False),
        sa.Column('versao', sa.Integer(), nullable=False),
        sa.Column('paperless_fluxo_id', sa.Integer(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), nullable=False),
        sa.Column('alterado_em', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('nome'),
    )
    op.create_table(
        'regra_documento_versao',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('regra_id', sa.Integer(), nullable=False),
        sa.Column('versao', sa.Integer(), nullable=False),
        sa.Column('dados', sa.Text(), nullable=False),
        sa.Column('utilizador_id', sa.Integer(), nullable=True),
        sa.Column('nota', sa.String(length=200), nullable=True),
        sa.Column('em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['regra_id'], ['regra_documento.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('regra_id', 'versao'),
    )
    op.create_index('ix_regra_documento_versao_regra_id', 'regra_documento_versao', ['regra_id'])
    op.create_table(
        'classificacao_documento',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('paperless_id', sa.Integer(), nullable=False),
        sa.Column('titulo', sa.String(length=200), nullable=False),
        sa.Column('adicionado', sa.Date(), nullable=True),
        sa.Column('estado', sa.String(length=12), nullable=False),
        sa.Column('origem', sa.String(length=10), nullable=True),
        sa.Column('regra_id', sa.Integer(), nullable=True),
        sa.Column('versao_regra', sa.Integer(), nullable=True),
        sa.Column('confianca', sa.String(length=10), nullable=True),
        sa.Column('proposta', sa.Text(), nullable=True),
        sa.Column('motivo', sa.Text(), nullable=True),
        sa.Column('movimento_id', sa.Integer(), nullable=True),
        sa.Column('processado_em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['regra_id'], ['regra_documento.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('paperless_id'),
    )
    op.create_index('ix_classificacao_documento_estado', 'classificacao_documento', ['estado'])
    op.create_index('ix_classificacao_documento_regra_id', 'classificacao_documento', ['regra_id'])
    op.create_table(
        'alteracao_documento',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('paperless_id', sa.Integer(), nullable=False),
        sa.Column('lote', sa.String(length=40), nullable=False),
        sa.Column('campo', sa.String(length=60), nullable=False),
        sa.Column('antes', sa.Text(), nullable=True),
        sa.Column('depois', sa.Text(), nullable=True),
        sa.Column('origem', sa.String(length=10), nullable=False),
        sa.Column('regra_id', sa.Integer(), nullable=True),
        sa.Column('versao_regra', sa.Integer(), nullable=True),
        sa.Column('utilizador_id', sa.Integer(), nullable=True),
        sa.Column('em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['regra_id'], ['regra_documento.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_alteracao_documento_paperless_id', 'alteracao_documento', ['paperless_id'])
    op.create_index('ix_alteracao_documento_lote', 'alteracao_documento', ['lote'])
    op.create_index('ix_alteracao_documento_em', 'alteracao_documento', ['em'])


def downgrade() -> None:
    op.drop_table('alteracao_documento')
    op.drop_table('classificacao_documento')
    op.drop_table('regra_documento_versao')
    op.drop_table('regra_documento')
