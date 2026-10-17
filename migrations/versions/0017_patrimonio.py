"""patrimonio: bens (casas, carros), avaliacoes e movimento.bem_id

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-17 23:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bem',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entidade_id', sa.Integer(), nullable=False),
    sa.Column('tipo', sa.String(length=10), nullable=False),
    sa.Column('nome', sa.String(length=100), nullable=False),
    sa.Column('uso', sa.String(length=20), nullable=False),
    sa.Column('tipologia', sa.Integer(), nullable=True),
    sa.Column('area_m2', sa.Integer(), nullable=True),
    sa.Column('localizacao', sa.String(length=200), nullable=True),
    sa.Column('ano', sa.Integer(), nullable=True),
    sa.Column('estado', sa.String(length=12), nullable=True),
    sa.Column('extras', sa.String(length=200), nullable=True),
    sa.Column('marca', sa.String(length=60), nullable=True),
    sa.Column('modelo', sa.String(length=60), nullable=True),
    sa.Column('km', sa.Integer(), nullable=True),
    sa.Column('combustivel', sa.String(length=16), nullable=True),
    sa.Column('data_aquisicao', sa.Date(), nullable=True),
    sa.Column('valor_aquisicao', sa.BigInteger(), nullable=True),
    sa.Column('emprestimo_conta_id', sa.Integer(), nullable=True),
    sa.Column('data_venda', sa.Date(), nullable=True),
    sa.Column('valor_venda', sa.BigInteger(), nullable=True),
    sa.Column('revisao_meses', sa.Integer(), server_default='6', nullable=False),
    sa.Column('padroes', sa.Text(), nullable=True),
    sa.Column('omissao', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('notas', sa.Text(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['emprestimo_conta_id'], ['conta.id'], name=op.f('fk_bem_emprestimo_conta_id_conta'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], name=op.f('fk_bem_entidade_id_entidade'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_bem'))
    )
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_bem_entidade_id'), ['entidade_id'], unique=False)
    op.create_table('avaliacao_bem',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('valor', sa.BigInteger(), nullable=False),
    sa.Column('minimo', sa.BigInteger(), nullable=True),
    sa.Column('maximo', sa.BigInteger(), nullable=True),
    sa.Column('fonte', sa.String(length=10), nullable=False),
    sa.Column('justificacao', sa.Text(), nullable=True),
    sa.Column('referencias', sa.Text(), nullable=True),
    sa.Column('confianca', sa.Float(), nullable=True),
    sa.Column('modelo', sa.String(length=60), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_avaliacao_bem_bem_id_bem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_avaliacao_bem'))
    )
    with op.batch_alter_table('avaliacao_bem', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_avaliacao_bem_bem_id'), ['bem_id'], unique=False)
    with op.batch_alter_table('movimento', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bem_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('bem_manual', sa.Boolean(), server_default='0', nullable=False))
        batch_op.create_index(batch_op.f('ix_movimento_bem_id'), ['bem_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_movimento_bem_id_bem'), 'bem', ['bem_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('movimento', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_movimento_bem_id_bem'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_movimento_bem_id'))
        batch_op.drop_column('bem_manual')
        batch_op.drop_column('bem_id')
    op.drop_table('avaliacao_bem')
    op.drop_table('bem')
