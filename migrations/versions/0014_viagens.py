"""viagens, modelos de viagem e o que se aprende com eles

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-17 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('modelo_viagem',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('nome', sa.String(length=80), nullable=False),
    sa.Column('dias_antes', sa.Integer(), nullable=False),
    sa.Column('dias_depois', sa.Integer(), nullable=False),
    sa.Column('tipos_conta', sa.String(length=200), nullable=False),
    sa.Column('contas', sa.String(length=200), nullable=False),
    sa.Column('incluir', sa.Text(), nullable=False),
    sa.Column('excluir', sa.Text(), nullable=False),
    sa.Column('categorias', sa.String(length=300), nullable=False),
    sa.Column('valor_min', sa.BigInteger(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_modelo_viagem')),
    sa.UniqueConstraint('nome', name=op.f('uq_modelo_viagem_nome'))
    )
    op.create_table('modelo_padrao',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('modelo_id', sa.Integer(), nullable=False),
    sa.Column('padrao', sa.String(length=200), nullable=False),
    sa.Column('positivos', sa.Integer(), nullable=False),
    sa.Column('negativos', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['modelo_id'], ['modelo_viagem.id'], name=op.f('fk_modelo_padrao_modelo_id_modelo_viagem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_modelo_padrao')),
    sa.UniqueConstraint('modelo_id', 'padrao', name=op.f('uq_modelo_padrao_modelo_id'))
    )
    with op.batch_alter_table('modelo_padrao', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_modelo_padrao_modelo_id'), ['modelo_id'], unique=False)
    op.create_table('viagem',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entidade_id', sa.Integer(), nullable=False),
    sa.Column('nome', sa.String(length=100), nullable=False),
    sa.Column('destino', sa.String(length=120), nullable=True),
    sa.Column('pais', sa.String(length=80), nullable=True),
    sa.Column('data_ini', sa.Date(), nullable=False),
    sa.Column('data_fim', sa.Date(), nullable=False),
    sa.Column('pessoas', sa.Integer(), nullable=False),
    sa.Column('motivo', sa.String(length=20), nullable=False),
    sa.Column('orcamento', sa.BigInteger(), nullable=True),
    sa.Column('moeda', sa.String(length=3), nullable=True),
    sa.Column('notas', sa.Text(), nullable=True),
    sa.Column('modelo_id', sa.Integer(), nullable=True),
    sa.Column('etiqueta_id', sa.Integer(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], name=op.f('fk_viagem_entidade_id_entidade'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['etiqueta_id'], ['etiqueta.id'], name=op.f('fk_viagem_etiqueta_id_etiqueta'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['modelo_id'], ['modelo_viagem.id'], name=op.f('fk_viagem_modelo_id_modelo_viagem'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_viagem'))
    )
    with op.batch_alter_table('viagem', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_viagem_entidade_id'), ['entidade_id'], unique=False)
    op.create_table('viagem_rejeitado',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('viagem_id', sa.Integer(), nullable=False),
    sa.Column('movimento_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_viagem_rejeitado_movimento_id_movimento'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['viagem_id'], ['viagem.id'], name=op.f('fk_viagem_rejeitado_viagem_id_viagem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_viagem_rejeitado')),
    sa.UniqueConstraint('viagem_id', 'movimento_id', name=op.f('uq_viagem_rejeitado_viagem_id'))
    )
    with op.batch_alter_table('viagem_rejeitado', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_viagem_rejeitado_viagem_id'), ['viagem_id'], unique=False)


def downgrade() -> None:
    op.drop_table('viagem_rejeitado')
    op.drop_table('viagem')
    op.drop_table('modelo_padrao')
    op.drop_table('modelo_viagem')
