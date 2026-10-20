"""Immich: chave pessoal da API por utilizador e álbum de cada viagem

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-20 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0036'
down_revision = '0035'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('utilizador', schema=None) as batch_op:
        batch_op.add_column(sa.Column('immich_token', sa.Text(), nullable=True))
    with op.batch_alter_table('viagem', schema=None) as batch_op:
        batch_op.add_column(sa.Column('immich_album_id', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('viagem', schema=None) as batch_op:
        batch_op.drop_column('immich_album_id')
    with op.batch_alter_table('utilizador', schema=None) as batch_op:
        batch_op.drop_column('immich_token')
