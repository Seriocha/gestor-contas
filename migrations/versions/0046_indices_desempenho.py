"""índices de desempenho: pares de movimentos (valor simétrico), saldos por conta e data, e as chaves estrangeiras
para movimento que o SQLite tem de verificar ao apagar um movimento (anular uma importação)

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-26 20:00:00
"""
from alembic import op


revision = '0046'
down_revision = '0045'
branch_labels = None
depends_on = None

# (nome, tabela, colunas) — os mesmos que a Administração › Desempenho sugere quando faltam
INDICES = [
    ("ix_movimento_montante_data", "movimento", "montante, data_mov"),      # sugestões de pares (−x ↔ +x)
    ("ix_movimento_conta_data", "movimento", "conta_id, data_mov"),         # saldos, extratos, relatórios por conta
    ("ix_movimento_par_id", "movimento", "par_id"),
    ("ix_movimento_importacao_id", "movimento", "importacao_id"),
    ("ix_movimento_regra_id", "movimento", "regra_id"),
    ("ix_importacao_conta_id", "importacao", "conta_id"),
    ("ix_par_ignorado_credito_id", "par_ignorado", "credito_id"),
    ("ix_viagem_rejeitado_movimento_id", "viagem_rejeitado", "movimento_id"),
    ("ix_viagem_confirmado_movimento_id", "viagem_confirmado", "movimento_id"),
    ("ix_manutencao_movimento_id", "manutencao", "movimento_id"),
    ("ix_classificacao_documento_movimento_id", "classificacao_documento", "movimento_id"),
]


def upgrade() -> None:
    for nome, tabela, colunas in INDICES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {nome} ON {tabela} ({colunas})")
    op.execute("ANALYZE")          # estatísticas para o planeador escolher os índices


def downgrade() -> None:
    for nome, _, _ in INDICES:
        op.execute(f"DROP INDEX IF EXISTS {nome}")
