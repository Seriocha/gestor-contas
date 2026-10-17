"""abastecimentos registados sem login, por validar

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-19 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('abastecimento_pendente',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('data', sa.Date(), nullable=False),
    sa.Column('hora', sa.String(length=5), nullable=True),
    sa.Column('km', sa.Integer(), nullable=True),
    sa.Column('litros', sa.Float(), nullable=False),
    sa.Column('cheio', sa.Boolean(), nullable=False),
    sa.Column('valor', sa.BigInteger(), nullable=True),
    sa.Column('posto', sa.String(length=120), nullable=True),
    sa.Column('notas', sa.String(length=300), nullable=True),
    sa.Column('quem', sa.String(length=60), nullable=True),
    sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_abastecimento_pendente_bem_id_bem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_abastecimento_pendente'))
    )
    with op.batch_alter_table('abastecimento_pendente', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_abastecimento_pendente_bem_id'), ['bem_id'], unique=False)


def downgrade() -> None:
    op.drop_table('abastecimento_pendente')
