"""contratos de eletricidade por casa (histórico de operadores; gestão por uma empresa como a Manie)

Revision ID: 0047
Revises: 0046
Create Date: 2026-10-27 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contrato_luz",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("bem_id", sa.Integer(), nullable=False),
        sa.Column("comercializador", sa.String(60), nullable=False),
        sa.Column("gestor", sa.String(60)),
        sa.Column("inicio", sa.Date(), nullable=False),
        sa.Column("fim", sa.Date()),
        sa.Column("tarifa", sa.String(12)),
        sa.Column("preco", sa.String(10)),
        sa.Column("potencia", sa.String(12)),
        sa.Column("cpe", sa.String(30)),
        sa.Column("referencia", sa.String(30)),
        sa.Column("texto_debito", sa.String(60)),
        sa.Column("motivo", sa.String(200)),
        sa.Column("notas", sa.Text()),
        sa.Column("criado_em", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["bem_id"], ["bem.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contrato_luz_bem_id", "contrato_luz", ["bem_id"])


def downgrade() -> None:
    op.drop_index("ix_contrato_luz_bem_id", "contrato_luz")
    op.drop_table("contrato_luz")
