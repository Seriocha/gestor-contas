"""módulo Garagem / Espaços: espaços, localizações, artigos e pré-registo (ligados a uma entidade e, opcionalmente, a um imóvel)

Revision ID: 0051
Revises: 0050
Create Date: 2026-11-16 18:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0051'
down_revision = '0050'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gar_espaco",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entidade_id", sa.Integer(), sa.ForeignKey("entidade.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("w", sa.Float(), nullable=False),
        sa.Column("d", sa.Float(), nullable=False),
        sa.Column("h", sa.Float(), nullable=False),
        sa.Column("bem_id", sa.Integer(), sa.ForeignKey("bem.id", ondelete="SET NULL"), nullable=True),
        sa.Column("imovel_nome", sa.String(120), nullable=True),
        sa.Column("imovel_ref", sa.String(120), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_gar_espaco_entidade_id", "gar_espaco", ["entidade_id"])
    op.create_index("ix_gar_espaco_bem_id", "gar_espaco", ["bem_id"])

    op.create_table(
        "gar_local",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entidade_id", sa.Integer(), sa.ForeignKey("entidade.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("gar_local.id"), nullable=True),
        sa.Column("space_id", sa.Integer(), sa.ForeignKey("gar_espaco.id", ondelete="SET NULL"), nullable=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("type", sa.String(20), nullable=False, server_default="zona"),
        sa.Column("code", sa.String(12), nullable=False, unique=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("photo", sa.String(40), nullable=True),
        sa.Column("grid_rows", sa.Integer(), nullable=True),
        sa.Column("grid_cols", sa.Integer(), nullable=True),
        sa.Column("grid_row", sa.Integer(), nullable=True),
        sa.Column("grid_col", sa.Integer(), nullable=True),
        sa.Column("plan_x", sa.Float(), nullable=True),
        sa.Column("plan_y", sa.Float(), nullable=True),
        sa.Column("plan_w", sa.Float(), nullable=True),
        sa.Column("plan_h", sa.Float(), nullable=True),
        sa.Column("plan_z", sa.Float(), nullable=True),
        sa.Column("plan_alt", sa.Float(), nullable=True),
        sa.Column("plan_wall", sa.String(10), nullable=True),
        sa.Column("plan_face", sa.String(10), nullable=True),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_gar_local_entidade_id", "gar_local", ["entidade_id"])
    op.create_index("ix_gar_local_parent_id", "gar_local", ["parent_id"])
    op.create_index("ix_gar_local_space_id", "gar_local", ["space_id"])

    op.create_table(
        "gar_artigo",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entidade_id", sa.Integer(), sa.ForeignKey("entidade.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.String(60), nullable=True),
        sa.Column("category", sa.String(80), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("photo", sa.String(40), nullable=True),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("gar_local.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated", sa.String(19), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_gar_artigo_entidade_id", "gar_artigo", ["entidade_id"])
    op.create_index("ix_gar_artigo_location_id", "gar_artigo", ["location_id"])

    op.create_table(
        "gar_rascunho",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entidade_id", sa.Integer(), sa.ForeignKey("entidade.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("quantity", sa.String(60), nullable=True),
        sa.Column("category", sa.String(80), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("location_id", sa.Integer(), sa.ForeignKey("gar_local.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created", sa.String(19), nullable=False),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_gar_rascunho_entidade_id", "gar_rascunho", ["entidade_id"])
    op.create_index("ix_gar_rascunho_location_id", "gar_rascunho", ["location_id"])

    # O módulo liga-se por entidade (Administração › Entidades). Fica logo ligado nas entidades pessoais já existentes.
    con = op.get_bind()
    for eid, modulos in con.execute(sa.text("SELECT id, modulos FROM entidade WHERE tipo = 'pessoal'")).all():
        lista = [m for m in (modulos or "").split(",") if m]
        if "garagem" not in lista:
            con.execute(sa.text("UPDATE entidade SET modulos = :m WHERE id = :i"), {"m": ",".join(lista + ["garagem"]), "i": eid})


def downgrade() -> None:
    con = op.get_bind()
    for eid, modulos in con.execute(sa.text("SELECT id, modulos FROM entidade")).all():
        lista = [m for m in (modulos or "").split(",") if m and m != "garagem"]
        con.execute(sa.text("UPDATE entidade SET modulos = :m WHERE id = :i"), {"m": ",".join(lista), "i": eid})
    op.drop_table("gar_rascunho")
    op.drop_table("gar_artigo")
    op.drop_table("gar_local")
    op.drop_table("gar_espaco")
