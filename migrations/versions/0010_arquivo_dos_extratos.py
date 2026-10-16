"""arquivo organizado dos extratos importados (funcionalidade retirada na v0.21.1;
migração mantida para que bases que já a aplicaram continuem a arrancar)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-16 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('importacao', schema=None) as batch_op:
        batch_op.add_column(sa.Column('arquivo', sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('importacao', schema=None) as batch_op:
        batch_op.drop_column('arquivo')
