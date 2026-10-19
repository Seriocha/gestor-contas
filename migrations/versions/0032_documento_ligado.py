"""documentos do Paperless-ngx ligados a movimentos e itens do recheio

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-19 12:00:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0032'
down_revision = '0031'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'documento_ligado',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('paperless_id', sa.Integer(), nullable=False),
        sa.Column('titulo', sa.String(length=200), nullable=False),
        sa.Column('correspondente', sa.String(length=120), nullable=True),
        sa.Column('data', sa.Date(), nullable=True),
        sa.Column('movimento_id', sa.Integer(), nullable=True),
        sa.Column('item_recheio_id', sa.Integer(), nullable=True),
        sa.Column('utilizador_id', sa.Integer(), nullable=True),
        sa.Column('criado_em', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.ForeignKeyConstraint(['movimento_id'], ['movimento.id'], name=op.f('fk_documento_ligado_movimento_id_movimento'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['item_recheio_id'], ['item_recheio.id'], name=op.f('fk_documento_ligado_item_recheio_id_item_recheio'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['utilizador_id'], ['utilizador.id'], name=op.f('fk_documento_ligado_utilizador_id_utilizador'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_documento_ligado')),
    )
    with op.batch_alter_table('documento_ligado', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_documento_ligado_paperless_id'), ['paperless_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_documento_ligado_movimento_id'), ['movimento_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_documento_ligado_item_recheio_id'), ['item_recheio_id'], unique=False)


def downgrade() -> None:
    op.drop_table('documento_ligado')
