"""Definições da app, lidas de variáveis de ambiente.

Todo o estado persistente vive em GESTOR_DATA_DIR (no container: /data):
a base SQLite, a chave das sessões e, por omissão, os backups.
"""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def data_dir_omissao() -> Path:
    return Path(os.environ.get("GESTOR_DATA_DIR") or BASE_DIR / "data")


@dataclass
class Definicoes:
    data_dir: Path
    backup_dir: Path
    secret_key: str
    cookie_secure: bool
    backup_intervalo_horas: int
    backup_retencao_dias: int
    # Pasta partilhada da NAS com os extratos (uma subpasta por banco), lida periodicamente. None = desligado.
    import_dir: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "gestor.db"


def _segredo_persistente(ficheiro: Path) -> str:
    """Gera a chave das sessões na 1.ª execução e reutiliza-a depois,
    para que um restart do container não termine as sessões abertas."""
    if ficheiro.exists():
        return ficheiro.read_text(encoding="utf-8").strip()
    chave = secrets.token_urlsafe(48)
    ficheiro.write_text(chave, encoding="utf-8")
    try:
        ficheiro.chmod(0o600)
    except OSError:
        pass
    return chave


def carregar_definicoes(data_dir: Path | None = None) -> Definicoes:
    data_dir = Path(data_dir) if data_dir else data_dir_omissao()
    data_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = Path(os.environ.get("GESTOR_BACKUP_DIR") or data_dir / "backup")
    return Definicoes(
        data_dir=data_dir,
        backup_dir=backup_dir,
        secret_key=os.environ.get("GESTOR_SECRET_KEY") or _segredo_persistente(data_dir / "secret.key"),
        cookie_secure=os.environ.get("GESTOR_COOKIE_SECURE", "0") == "1",
        backup_intervalo_horas=int(os.environ.get("GESTOR_BACKUP_INTERVALO_HORAS", "24")),
        backup_retencao_dias=int(os.environ.get("GESTOR_BACKUP_RETENCAO_DIAS", "30")),
        import_dir=Path(os.environ["GESTOR_IMPORT_DIR"]) if os.environ.get("GESTOR_IMPORT_DIR") else None,
    )
