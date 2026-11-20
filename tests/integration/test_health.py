from __future__ import annotations

import logging

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app import health
from app.api.main import app


@pytest.fixture()
def client(services):
    with TestClient(app) as c:
        yield c


def _redis_that_refuses():
    # Port 1 refuses connections: a genuine failure, not a mocked exception.
    return redis.Redis(port=1, socket_connect_timeout=0.2)


def _postgres_that_refuses():
    return create_engine(
        "postgresql+psycopg://nordcart:nordcart@127.0.0.1:1/nordcart",
        poolclass=NullPool,
        connect_args={"connect_timeout": 2},
    )


def test_health_is_200_when_postgres_and_redis_answer(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


def test_health_is_503_and_names_redis_when_redis_is_down(client, monkeypatch, caplog):
    monkeypatch.setattr(health, "get_redis", _redis_that_refuses)
    with caplog.at_level(logging.WARNING, logger="app.health"):
        r = client.get("/health")

    assert r.status_code == 503
    assert r.json() == {
        "status": "degraded",
        "postgres": "ok",
        "redis": "down",
        "failed": ["redis"],
    }
    assert "redis check failed" in caplog.text
    assert "Error" not in r.text, "exception details go to the log, not to an open endpoint"


def test_health_is_503_and_names_postgres_when_postgres_is_down(client, monkeypatch, caplog):
    monkeypatch.setattr(health, "_probe_engine", _postgres_that_refuses)
    with caplog.at_level(logging.WARNING, logger="app.health"):
        r = client.get("/health")

    assert r.status_code == 503
    assert r.json() == {
        "status": "degraded",
        "postgres": "down",
        "redis": "ok",
        "failed": ["postgres"],
    }
    assert "postgres check failed" in caplog.text
    assert "127.0.0.1" not in r.text


def test_health_names_every_failing_component(client, monkeypatch):
    monkeypatch.setattr(health, "_probe_engine", _postgres_that_refuses)
    monkeypatch.setattr(health, "get_redis", _redis_that_refuses)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["failed"] == ["postgres", "redis"]


def test_requests_are_still_served_from_postgres_while_redis_is_down(client, monkeypatch):
    """/health reports the outage; the cache itself still fails open (see test_cache)."""
    monkeypatch.setattr(health, "get_redis", _redis_that_refuses)
    assert client.get("/health").status_code == 503
    assert client.get("/api/v1/sellers/1/status").status_code == 401  # the API itself answers
