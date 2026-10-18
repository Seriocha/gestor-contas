"""recheio das casas: categorias de recheio, itens, pedidos de garantia, capital seguro do recheio

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-18 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0027'
down_revision = '0026'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.add_column(sa.Column('recheio', sa.Boolean(), server_default='0', nullable=False))
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('capital_recheio', sa.BigInteger(), nullable=True))
    op.create_table('item_recheio',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('entidade_id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=True),
    sa.Column('movimento_id', sa.Integer(), nullable=True),
    sa.Column('nome', sa.String(length=120), nullable=False),
    sa.Column('tipo', sa.String(length=16), nullable=True),
    sa.Column('marca', sa.String(length=60), nullable=True),
    sa.Column('modelo', sa.String(length=80), nullable=True),
    sa.Column('n_serie', sa.String(length=60), nullable=True),
    sa.Column('atributos', sa.Text(), nullable=True),
    sa.Column('data_compra', sa.Date(), nullable=True),
    sa.Column('valor', sa.BigInteger(), nullable=True),
    sa.Column('garantia_meses', sa.Integer(), server_default='36', nullable=False),
    sa.Column('loja', sa.String(length=120), nullable=True),
    sa.Column('contacto_telefone', sa.String(length=40), nullable=True),
    sa.Column('contacto_email', sa.String(length=120), nullable=True),
    sa.Column('contacto_site', sa.String(length=200), nullable=True),
    sa.Column('estado', sa.String(length=10), server_default='ativo', nullable=False),
    sa.Column('notas', sa.Text(), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], name=op.f('fk_item_recheio_entidade_id_entidade'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_item_recheio_bem_id_bem'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_item_recheio_movimento_id_movimento'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item_recheio'))
    )
    with op.batch_alter_table('item_recheio', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_item_recheio_entidade_id'), ['entidade_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_item_recheio_bem_id'), ['bem_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_item_recheio_movimento_id'), ['movimento_id'], unique=False)
    op.create_table('pedido_garantia',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('problema', sa.Text(), nullable=False),
    sa.Column('referencia', sa.String(length=60), nullable=True),
    sa.Column('estado', sa.String(length=10), nullable=False),
    sa.Column('resolucao', sa.Text(), nullable=True),
    sa.Column('data_fecho', sa.Date(), nullable=True),
    sa.ForeignKeyConstraint(['item_id'], ['item_recheio.id'], name=op.f('fk_pedido_garantia_item_id_item_recheio'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_pedido_garantia'))
    )
    with op.batch_alter_table('pedido_garantia', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_pedido_garantia_item_id'), ['item_id'], unique=False)


def downgrade() -> None:
    op.drop_table('pedido_garantia')
    op.drop_table('item_recheio')
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.drop_column('capital_recheio')
    with op.batch_alter_table('categoria', schema=None) as batch_op:
        batch_op.drop_column('recheio')
