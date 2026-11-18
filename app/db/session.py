from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        # Every connection runs in UTC so `day` boundaries never depend on server settings.
        connect_args={"options": "-c timezone=UTC"},
    )


@lru_cache
def _sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(get_engine(), expire_on_commit=False)


@contextmanager
def session_scope() -> Iterator[Session]:
    """One transaction: commit on success, roll back on error."""
    with _sessionmaker()() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def get_session() -> Iterator[Session]:
    """FastAPI dependency. Read endpoints never commit; writers call `session.commit()`."""
    with _sessionmaker()() as session:
        yield session
