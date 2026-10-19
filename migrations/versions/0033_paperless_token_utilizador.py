"""token pessoal do Paperless-ngx por utilizador (cifrado)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-19 20:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0033'
down_revision = '0032'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('utilizador', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paperless_token', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('utilizador', schema=None) as batch_op:
        batch_op.drop_column('paperless_token')
