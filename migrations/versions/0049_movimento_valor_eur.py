"""valor em euros dos movimentos noutra moeda (dado pelo próprio extrato)

Revision ID: 0049
Revises: 0048
Create Date: 2026-11-02 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0049'
down_revision = '0048'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("movimento") as b:
        b.add_column(sa.Column("valor_eur", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("movimento") as b:
        b.drop_column("valor_eur")
