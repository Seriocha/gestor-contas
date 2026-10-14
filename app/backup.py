"""Backup periódico da base SQLite (API de backup online do sqlite3 — seguro
com a app a correr) para GESTOR_BACKUP_DIR, com retenção em dias."""
import asyncio
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .config import Definicoes

log = logging.getLogger("gestor.backup")
_PREFIXO = "gestor-"


def fazer_backup(defs: Definicoes) -> Path:
    defs.backup_dir.mkdir(parents=True, exist_ok=True)
    destino = defs.backup_dir / f"{_PREFIXO}{datetime.now():%Y%m%d-%H%M%S}.db"
    origem = sqlite3.connect(defs.db_path)
    try:
        alvo = sqlite3.connect(destino)
        try:
            origem.backup(alvo)
        finally:
            alvo.close()
    finally:
        origem.close()
    limpar_antigos(defs)
    log.info("Backup criado: %s", destino)
    return destino


def limpar_antigos(defs: Definicoes) -> None:
    limite = time.time() - defs.backup_retencao_dias * 86400
    for f in defs.backup_dir.glob(f"{_PREFIXO}*.db"):
        if f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)


def ultimo_backup(defs: Definicoes) -> datetime | None:
    ficheiros = sorted(defs.backup_dir.glob(f"{_PREFIXO}*.db")) if defs.backup_dir.exists() else []
    return datetime.fromtimestamp(ficheiros[-1].stat().st_mtime) if ficheiros else None


async def ciclo_backups(defs: Definicoes) -> None:
    """Verifica de hora a hora; faz backup quando o último tem mais do que o intervalo."""
    if defs.backup_intervalo_horas <= 0:
        return
    while True:
        try:
            ultimo = ultimo_backup(defs)
            if ultimo is None or (datetime.now() - ultimo).total_seconds() >= defs.backup_intervalo_horas * 3600:
                await asyncio.to_thread(fazer_backup, defs)
        except Exception:
            log.exception("Falha no backup automático")
        await asyncio.sleep(3600)
