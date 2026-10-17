"""construcoes das propriedades rusticas

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-19 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('construcao',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('bem_id', sa.Integer(), nullable=False),
    sa.Column('designacao', sa.String(length=100), nullable=False),
    sa.Column('tipo', sa.String(length=12), nullable=True),
    sa.Column('artigo', sa.String(length=60), nullable=True),
    sa.Column('area_terreno', sa.Float(), nullable=True),
    sa.Column('area_bruta', sa.Float(), nullable=True),
    sa.Column('estado', sa.String(length=12), nullable=True),
    sa.Column('ordem', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['bem_id'], ['bem.id'], name=op.f('fk_construcao_bem_id_bem'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_construcao'))
    )
    with op.batch_alter_table('construcao', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_construcao_bem_id'), ['bem_id'], unique=False)


def downgrade() -> None:
    op.drop_table('construcao')
