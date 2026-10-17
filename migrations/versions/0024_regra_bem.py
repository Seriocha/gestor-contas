"""regra: ligar ao bem (casa, propriedade, carro)

Revision ID: 0024
Revises: 0023
Create Date: 2026-10-19 19:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0024'
down_revision = '0023'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('regra', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bem_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_regra_bem_id_bem'), 'bem', ['bem_id'], ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('regra', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_regra_bem_id_bem'), type_='foreignkey')
        batch_op.drop_column('bem_id')
