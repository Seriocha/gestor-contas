"""ligações dos recibos de vencimento ao Paperless (pessoa, tipo de documento, empregador, entidade)

Revision ID: 0050
Revises: 0049
Create Date: 2026-11-06 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ligacao_recibos",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("nome", sa.String(80), nullable=False),
        sa.Column("utilizador_id", sa.Integer(), sa.ForeignKey("utilizador.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entidade_id", sa.Integer(), sa.ForeignKey("entidade.id", ondelete="SET NULL"), nullable=True),
        sa.Column("tipo_documento", sa.String(128), nullable=False),
        sa.Column("correspondente", sa.String(128), nullable=True),
        sa.Column("ativo", sa.Boolean(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("ligacao_recibos")
