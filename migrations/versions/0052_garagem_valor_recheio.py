"""Garagem: artigos de valor e ligação ao recheio da casa

Revision ID: 0052
Revises: 0051
Create Date: 2026-11-17 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0052'
down_revision = '0051'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gar_artigo") as b:
        b.add_column(sa.Column("valuable", sa.Boolean(), nullable=False, server_default="0"))
        b.add_column(sa.Column("recheio_id", sa.Integer(), sa.ForeignKey("item_recheio.id", ondelete="SET NULL"), nullable=True))
        b.create_index("ix_gar_artigo_recheio_id", ["recheio_id"])


def downgrade() -> None:
    with op.batch_alter_table("gar_artigo") as b:
        b.drop_index("ix_gar_artigo_recheio_id")
        b.drop_column("recheio_id")
        b.drop_column("valuable")
