"""extratos da pasta partilhada à espera de validação

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-18 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0029'
down_revision = '0028'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'importacao_pendente',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('pasta', sa.String(length=120), nullable=False),
        sa.Column('ficheiro', sa.String(length=255), nullable=False),
        sa.Column('caminho', sa.String(length=500), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('parser', sa.String(length=30), nullable=True),
        sa.Column('banco', sa.String(length=60), nullable=True),
        sa.Column('conta_id', sa.Integer(), nullable=True),
        sa.Column('deteccao', sa.String(length=200), nullable=True),
        sa.Column('n_lidos', sa.Integer(), nullable=False),
        sa.Column('n_novos', sa.Integer(), nullable=True),
        sa.Column('data_ini', sa.Date(), nullable=True),
        sa.Column('data_fim', sa.Date(), nullable=True),
        sa.Column('erro', sa.String(length=500), nullable=True),
        sa.Column('estado', sa.String(length=12), nullable=False),
        sa.Column('nota', sa.String(length=300), nullable=True),
        sa.Column('detetado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('tratado_em', sa.DateTime(), nullable=True),
        sa.Column('tratado_por', sa.Integer(), nullable=True),
        sa.Column('importacao_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['conta_id'], ['conta.id'], name=op.f('fk_importacao_pendente_conta_id_conta'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['tratado_por'], ['utilizador.id'], name=op.f('fk_importacao_pendente_tratado_por_utilizador'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['importacao_id'], ['importacao.id'], name=op.f('fk_importacao_pendente_importacao_id_importacao'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_importacao_pendente')),
    )
    with op.batch_alter_table('importacao_pendente', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_importacao_pendente_sha256'), ['sha256'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('importacao_pendente', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_importacao_pendente_sha256'))
    op.drop_table('importacao_pendente')
