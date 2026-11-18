from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://nordcart:nordcart@localhost:5443/nordcart"
    redis_url: str = "redis://localhost:6390/0"
    celery_broker_url: str = "amqp://nordcart:nordcart@localhost:5673//"
    celery_result_backend: str = "redis://localhost:6390/1"
    # Run tasks inline instead of sending them to RabbitMQ (tests, one-off scripts).
    celery_eager: bool = False

    # No default on purpose: the service refuses to start without a real key.
    admin_api_key: str

    cache_ttl_seconds: int = 300
    cache_lock_seconds: int = 20
    max_range_days: int = 366
    # How often the beat scheduler looks for sellers with unprocessed new data.
    beat_interval_seconds: int = 60

    @field_validator("admin_api_key")
    @classmethod
    def _strong_admin_key(cls, v: str) -> str:
        if len(v) < 24:
            raise ValueError("ADMIN_API_KEY must be at least 24 characters (see .env.example)")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
