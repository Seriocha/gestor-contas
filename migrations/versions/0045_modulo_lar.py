"""módulo «lar» (conciliação da Santa Casa) ligado nas entidades de herança

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-26 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0045'
down_revision = '0044'
branch_labels = None
depends_on = None


def upgrade() -> None:
    con = op.get_bind()
    for eid, modulos in con.execute(sa.text("SELECT id, modulos FROM entidade WHERE tipo = 'heranca'")).all():
        lista = [m for m in (modulos or "").split(",") if m]
        if "lar" not in lista:
            con.execute(sa.text("UPDATE entidade SET modulos = :m WHERE id = :i"), {"m": ",".join(lista + ["lar"]), "i": eid})


def downgrade() -> None:
    con = op.get_bind()
    for eid, modulos in con.execute(sa.text("SELECT id, modulos FROM entidade")).all():
        lista = [m for m in (modulos or "").split(",") if m and m != "lar"]
        con.execute(sa.text("UPDATE entidade SET modulos = :m WHERE id = :i"), {"m": ",".join(lista), "i": eid})
