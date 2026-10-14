from alembic import context
from sqlalchemy import create_engine, pool

from app import models  # noqa: F401  (regista as tabelas no metadata)
from app.config import data_dir_omissao
from app.db import Base, url_sqlite

config = context.config
url = config.get_main_option("sqlalchemy.url") or url_sqlite(data_dir_omissao() / "gestor.db")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
