"""manutencao dos carros: registos de km, plano (IA) e idas a oficina

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-18 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('plano_notas', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('plano_em', sa.Date(), nullable=True))
        batch_op.add_column(sa.Column('plano_modelo', sa.String(length=60), nullable=True))
    op.create_table('registo_km',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('km', sa.Integer(), nullable=False),
    sa.Column('origem', sa.String(length=10), nullable=False),
    sa.Column('nota', sa.String(length=200), nullable=True),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_registo_km_bem_id_bem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_registo_km'))
    )
    with op.batch_alter_table('registo_km', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_registo_km_bem_id'), ['bem_id'], unique=False)
    op.create_table('item_plano',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('item', sa.String(length=120), nullable=False),
    sa.Column('intervalo_km', sa.Integer(), nullable=True),
    sa.Column('intervalo_meses', sa.Integer(), nullable=True),
    sa.Column('custo_min', sa.BigInteger(), nullable=True),
    sa.Column('custo_max', sa.BigInteger(), nullable=True),
    sa.Column('notas', sa.String(length=300), nullable=True),
    sa.Column('ordem', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_item_plano_bem_id_bem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_item_plano'))
    )
    with op.batch_alter_table('item_plano', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_item_plano_bem_id'), ['bem_id'], unique=False)
    op.create_table('manutencao',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('km', sa.Integer(), nullable=True),
    sa.Column('itens', sa.Text(), nullable=True),
    sa.Column('custo', sa.BigInteger(), nullable=True),
    sa.Column('movimento_id', sa.Integer(), nullable=True),
    sa.Column('nota', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_manutencao_bem_id_bem'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_manutencao_movimento_id_movimento'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_manutencao'))
    )
    with op.batch_alter_table('manutencao', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_manutencao_bem_id'), ['bem_id'], unique=False)


def downgrade() -> None:
    op.drop_table('manutencao')
    op.drop_table('item_plano')
    op.drop_table('registo_km')
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.drop_column('plano_modelo')
        batch_op.drop_column('plano_em')
        batch_op.drop_column('plano_notas')
