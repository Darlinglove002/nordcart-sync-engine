from __future__ import annotations

import os
from pathlib import Path

import pytest
import redis
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from tests.conftest import TEST_DB_URL, TEST_REDIS_URL

ROOT = Path(__file__).resolve().parents[2]
TABLES = "daily_sku_metrics, daily_seller_metrics, ad_spend, returns, orders, products, sellers"


def _unavailable(reason: str):
    # CI sets REQUIRE_SERVICES=1 so a missing service is a failure, never a silent skip.
    if os.environ.get("REQUIRE_SERVICES") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture(scope="session")
def services():
    url = make_url(TEST_DB_URL)
    hint = "run `docker compose up -d db redis`"
    try:
        admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
        with admin.connect() as c:
            exists = c.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": url.database}
            ).scalar()
            if not exists:
                c.execute(text(f'CREATE DATABASE "{url.database}"'))
        admin.dispose()
    except Exception as e:
        _unavailable(f"PostgreSQL not reachable ({type(e).__name__}); {hint}")
    try:
        redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1).ping()
    except Exception as e:
        _unavailable(f"Redis not reachable ({type(e).__name__}); {hint}")

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    command.downgrade(cfg, "base")  # proves the migration applies and reverts cleanly
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_state(services):
    """Every integration test starts with empty tables and an empty Redis db."""
    from app.db.session import get_engine

    with get_engine().begin() as c:
        c.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    redis.Redis.from_url(TEST_REDIS_URL).flushdb()
    yield


@pytest.fixture()
def engine(services):
    from app.db.session import get_engine

    return get_engine()


@pytest.fixture()
def r():
    return redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
