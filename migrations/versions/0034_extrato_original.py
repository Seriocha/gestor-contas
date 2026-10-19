"""extrato original de cada importação (Paperless / pasta partilhada) e extratos vindos do Paperless

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-19 22:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0034'
down_revision = '0033'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('importacao', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paperless_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('origem', sa.String(length=500), nullable=True))
        batch_op.create_index('ix_importacao_paperless_id', ['paperless_id'], unique=False)
    with op.batch_alter_table('importacao_pendente', schema=None) as batch_op:
        batch_op.add_column(sa.Column('paperless_id', sa.Integer(), nullable=True))
        batch_op.create_index('ix_importacao_pendente_paperless_id', ['paperless_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('importacao_pendente', schema=None) as batch_op:
        batch_op.drop_index('ix_importacao_pendente_paperless_id')
        batch_op.drop_column('paperless_id')
    with op.batch_alter_table('importacao', schema=None) as batch_op:
        batch_op.drop_index('ix_importacao_paperless_id')
        batch_op.drop_column('origem')
        batch_op.drop_column('paperless_id')
