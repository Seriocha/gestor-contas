"""entidade pessoal/heranca com quota; propriedades rusticas (ocupacao, agua, cortica)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-18 21:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('entidade', schema=None) as batch_op:
        batch_op.add_column(sa.Column('tipo', sa.String(length=10), server_default='pessoal', nullable=False))
        batch_op.add_column(sa.Column('quota', sa.Float(), nullable=True))
    with op.batch_alter_table('bem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('area_ha', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('artigo', sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column('ocupacao', sa.String(length=300), nullable=True))
        batch_op.add_column(sa.Column('agua', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('acesso', sa.String(length=12), nullable=True))
        batch_op.add_column(sa.Column('tem_cortica', sa.Boolean(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('anos_tiragem', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('sobreiros', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('producao_arrobas', sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('bem', schema=None) as batch_op:
        for c in ('producao_arrobas', 'sobreiros', 'anos_tiragem', 'tem_cortica', 'acesso', 'agua', 'ocupacao',
                  'artigo', 'area_ha'):
            batch_op.drop_column(c)
    with op.batch_alter_table('entidade', schema=None) as batch_op:
        batch_op.drop_column('quota')
        batch_op.drop_column('tipo')
