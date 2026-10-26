import re
from pathlib import Path

from sqlalchemy import MetaData, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase

# Nomes de constraints estáveis: o Alembic precisa deles para alterar
# tabelas em SQLite (modo batch).
_CONVENCAO = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=_CONVENCAO)


# Versão dos movimentos e contas de cada base: sobe quando uma ligação que escreveu nessas tabelas faz commit e
# quando volta ao pool (já com o commit feito). As caches em memória (histórico dos saldos) comparam-na.
_VERSOES: dict[str, int] = {}
_ESCRITA = re.compile(r"^\s*(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
_TABELAS_VERSIONADAS = re.compile(r"\b(?:movimento|conta)\b", re.IGNORECASE)


def versao_dados(url) -> int:
    return _VERSOES.get(str(url), 0)


def url_sqlite(db_path: Path) -> str:
    return f"sqlite:///{Path(db_path).as_posix()}"


def criar_engine(db_path: Path) -> Engine:
    engine = create_engine(url_sqlite(db_path), connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    chave = str(engine.url)

    def _subir():
        _VERSOES[chave] = _VERSOES.get(chave, 0) + 1

    @event.listens_for(engine, "before_cursor_execute")
    def _marcar(conn, cursor, statement, parameters, context, executemany):
        if _ESCRITA.match(statement) and _TABELAS_VERSIONADAS.search(statement):
            conn.connection.info["mexeu"] = True

    @event.listens_for(engine, "commit")
    def _no_commit(conn):
        if conn.connection.info.get("mexeu"):
            _subir()

    @event.listens_for(engine.pool, "checkin")
    def _devolvida(dbapi_conn, registo):
        if registo is not None and registo.info.pop("mexeu", False):
            _subir()

    return engine
