"""contratos dos serviços da casa (luz, gás, água) e a casa de cada um, para comparar fornecedores por casa

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-20 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0038'
down_revision = '0037'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'contrato_servico',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('entidade_id', sa.Integer(), nullable=False),
        sa.Column('bem_id', sa.Integer(), nullable=False),
        sa.Column('texto', sa.String(length=60), nullable=False),
        sa.Column('referencia', sa.String(length=30), nullable=True),
        sa.Column('dia_de', sa.Integer(), nullable=True),
        sa.Column('dia_ate', sa.Integer(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['entidade_id'], ['entidade.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('contrato_servico', schema=None) as batch_op:
        batch_op.create_index('ix_contrato_servico_entidade_id', ['entidade_id'], unique=False)
        batch_op.create_index('ix_contrato_servico_bem_id', ['bem_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('contrato_servico', schema=None) as batch_op:
        batch_op.drop_index('ix_contrato_servico_bem_id')
        batch_op.drop_index('ix_contrato_servico_entidade_id')
    op.drop_table('contrato_servico')
