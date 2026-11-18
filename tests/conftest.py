"""Test environment. Must run before any `app` module reads settings.

Integration tests use a throw-away database (`nordcart_test`) and Redis db 15, never the
development data. CI points TEST_DATABASE_URL / TEST_REDIS_URL at service containers.
"""

import os

TEST_DB_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://nordcart:nordcart@localhost:5443/nordcart_test"
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6390/15")

os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ["ALEMBIC_DATABASE_URL"] = TEST_DB_URL
os.environ["REDIS_URL"] = TEST_REDIS_URL
os.environ["CELERY_RESULT_BACKEND"] = TEST_REDIS_URL.rsplit("/", 1)[0] + "/14"
os.environ["CELERY_EAGER"] = "true"  # tasks run inline; no RabbitMQ in tests
os.environ["ADMIN_API_KEY"] = "test-admin-key-0123456789abcdef-xyz"
