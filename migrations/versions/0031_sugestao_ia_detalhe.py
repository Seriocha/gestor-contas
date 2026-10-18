"""análise de um movimento pela IA: detalhe (JSON) e se usou pesquisa web

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-19 00:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0031'
down_revision = '0030'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('sugestao_ia', schema=None) as batch_op:
        batch_op.add_column(sa.Column('detalhe', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('web', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('sugestao_ia', schema=None) as batch_op:
        batch_op.drop_column('web')
        batch_op.drop_column('detalhe')
