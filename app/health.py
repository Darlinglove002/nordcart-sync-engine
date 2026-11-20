"""Dependency checks behind `GET /health`."""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.cache import get_redis
from app.config import get_settings

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_SECONDS = 2  # libpq takes whole seconds and rounds anything below 2 up to 2
STATEMENT_TIMEOUT_MS = 2000


@lru_cache
def _probe_engine() -> Engine:
    """An engine of its own with no pool: every probe is a fresh connection with hard timeouts.

    A pooled connection that was fine a minute ago is handed out as if healthy, and on a frozen
    server `SELECT 1` over it never returns, so the probe would hang instead of failing.
    """
    return create_engine(
        get_settings().database_url,
        poolclass=NullPool,
        connect_args={
            "connect_timeout": CONNECT_TIMEOUT_SECONDS,
            "options": f"-c timezone=UTC -c statement_timeout={STATEMENT_TIMEOUT_MS}",
        },
    )


def check_postgres() -> bool:
    """True if PostgreSQL answers `SELECT 1` on a fresh connection within a few seconds."""
    try:
        with _probe_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # any failure means "not ready"; the class goes to the log only
        log.warning("postgres check failed: %s", type(exc).__name__)
        return False
    return True


def check_redis() -> bool:
    """True if Redis answers PING. The shared client already has 0.5 s socket timeouts."""
    try:
        get_redis().ping()
    except Exception as exc:
        log.warning("redis check failed: %s", type(exc).__name__)
        return False
    return True


def run_checks() -> dict[str, bool]:
    """Component name -> healthy. Add a line here when the service gains a dependency."""
    return {"postgres": check_postgres(), "redis": check_redis()}
