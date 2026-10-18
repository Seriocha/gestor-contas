"""movimento associado a uma subscricao

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-18 16:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0028'
down_revision = '0027'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('movimento', schema=None) as batch_op:
        batch_op.add_column(sa.Column('subscricao_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_movimento_subscricao_id'), ['subscricao_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_movimento_subscricao_id_subscricao'), 'subscricao', ['subscricao_id'],
                                    ['id'], ondelete='SET NULL')


def downgrade() -> None:
    with op.batch_alter_table('movimento', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_movimento_subscricao_id_subscricao'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_movimento_subscricao_id'))
        batch_op.drop_column('subscricao_id')
