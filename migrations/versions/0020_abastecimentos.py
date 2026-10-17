"""abastecimentos importados do Fuelio

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-18 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('abastecimento',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('hora', sa.String(length=5), nullable=True),
    sa.Column('km', sa.Integer(), nullable=True),
    sa.Column('litros', sa.Float(), nullable=False),
    sa.Column('cheio', sa.Boolean(), nullable=False),
    sa.Column('valor', sa.BigInteger(), nullable=True),
    sa.Column('preco_litro', sa.Float(), nullable=True),
    sa.Column('posto', sa.String(length=120), nullable=True),
    sa.Column('notas', sa.String(length=300), nullable=True),
    sa.Column('guid', sa.String(length=64), nullable=False),
    sa.Column('movimento_id', sa.Integer(), nullable=True),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_abastecimento_bem_id_bem'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_abastecimento_movimento_id_movimento'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_abastecimento')),
    sa.UniqueConstraint('bem_id', 'guid', name=op.f('uq_abastecimento_bem_id'))
    )
    with op.batch_alter_table('abastecimento', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_abastecimento_bem_id'), ['bem_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_abastecimento_data'), ['data'], unique=False)
        batch_op.create_index(batch_op.f('ix_abastecimento_movimento_id'), ['movimento_id'], unique=False)


def downgrade() -> None:
    op.drop_table('abastecimento')
