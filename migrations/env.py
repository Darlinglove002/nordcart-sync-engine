import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    # Plain os.environ on purpose: migrations must not require the API's ADMIN_API_KEY.
    # Tests set ALEMBIC_DATABASE_URL to point at the throw-away test database.
    url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("Set DATABASE_URL (see .env.example)")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
