"""perfil de importação de cada entidade: bancos incluídos e fontes automáticas de extratos

Revision ID: 0048
Revises: 0047
Create Date: 2026-10-29 10:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("entidade") as b:
        b.add_column(sa.Column("bancos", sa.String(300), nullable=False, server_default=""))
        b.add_column(sa.Column("fontes", sa.String(40), nullable=False, server_default="pasta,paperless"))


def downgrade() -> None:
    with op.batch_alter_table("entidade") as b:
        b.drop_column("fontes")
        b.drop_column("bancos")
