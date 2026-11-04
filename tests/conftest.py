"""Aceleração da suite: só afeta os testes, nunca a app em produção.

- As migrações do Alembic correm uma vez por sessão para uma base de dados
  «modelo»; cada create_app() numa pasta sem gestor.db recebe uma cópia dela
  em vez de voltar a correr as migrações todas.
- Parâmetros do argon2 mínimos (hash continua válido, só mais barato).
"""
import shutil
import sqlite3
from pathlib import Path

import pytest
from argon2 import PasswordHasher

from app import main, seguranca

seguranca._ph = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)

_migrar_real = main.aplicar_migracoes


@pytest.fixture(scope="session")
def _bd_modelo(tmp_path_factory) -> Path:
    modelo = tmp_path_factory.mktemp("bd_modelo") / "gestor.db"
    _migrar_real(modelo)
    with sqlite3.connect(modelo) as con:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return modelo


@pytest.fixture(autouse=True)
def _migracoes_rapidas(monkeypatch, _bd_modelo):
    def aplicar(db_path: Path, backup_dir: Path | None = None) -> None:
        db_path = Path(db_path)
        if db_path.exists():
            _migrar_real(db_path, backup_dir)  # BD preexistente (ex.: restauro/versão antiga): migrar a sério
        else:
            shutil.copyfile(_bd_modelo, db_path)

    monkeypatch.setattr(main, "aplicar_migracoes", aplicar)
