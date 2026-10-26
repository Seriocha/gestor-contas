"""Lar: documentos validados à mão; regularização declarada pelo utilizador (pago até 31/10/2026, validar desde 2026)

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-26 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0044'
down_revision = '0043'
branch_labels = None
depends_on = None

# O utilizador declarou (2026-10-26) que está tudo pago até este mês e que os documentos de 2026 ficam por validar.
# Só se grava se ainda não houver valor (mudam-se em Lar › Configurar).
PREFS = {"lar_pago_ate": "2026-10-31", "lar_validar_desde": "2026-01-01"}


def upgrade() -> None:
    op.create_table(
        'lar_validacao',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chave', sa.String(length=30), nullable=False),
        sa.Column('nota', sa.String(length=200), nullable=True),
        sa.Column('utilizador_id', sa.Integer(), nullable=True),
        sa.Column('em', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chave'),
    )
    con = op.get_bind()
    for chave, valor in PREFS.items():
        existe = con.execute(sa.text("SELECT 1 FROM definicao WHERE chave = :c"), {"c": chave}).first()
        if not existe:
            con.execute(sa.text("INSERT INTO definicao (chave, valor) VALUES (:c, :v)"), {"c": chave, "v": valor})


def downgrade() -> None:
    op.drop_table('lar_validacao')
