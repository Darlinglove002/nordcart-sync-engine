"""Redis read-through cache for dashboard payloads.

Design points (each one is covered by a test):

* **Version-based invalidation.** Keys embed a per-seller version (`ue:ver:{seller}`).
  Invalidating a seller is one atomic INCR; old keys become unreachable and expire by TTL.
  No SCAN/DEL over a keyspace, no window where half the keys are stale.
* **Single flight.** On a miss one caller takes a short Redis lock and computes; concurrent
  callers wait briefly and read the leader's result. A popular dashboard that just expired
  does not turn into N identical queries.
* **Fail open.** If Redis is down the request is served from PostgreSQL. A cache outage
  costs latency, never availability.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import redis
from redis.exceptions import LockError, RedisError

from app.config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(
        get_settings().redis_url,
        socket_timeout=0.5,
        socket_connect_timeout=0.5,
        decode_responses=True,
    )


def _version_key(seller_id: int) -> str:
    return f"ue:ver:{seller_id}"


class DashboardCache:
    def __init__(self, client: redis.Redis, ttl: int, lock_seconds: int, wait_seconds: float = 3.0):
        self.r = client
        self.ttl = ttl
        self.lock_seconds = lock_seconds
        self.wait_seconds = wait_seconds

    def key(self, seller_id: int, *parts: object) -> str:
        version = self.r.get(_version_key(seller_id)) or "0"
        return f"ue:{seller_id}:v{version}:" + ":".join(str(p) for p in parts)

    def invalidate(self, seller_id: int) -> None:
        """Make every cached payload for this seller unreachable. Call AFTER the DB commit."""
        try:
            self.r.incr(_version_key(seller_id))
        except RedisError:
            log.warning("cache invalidation failed for seller %s; entries expire by TTL", seller_id)

    def get_or_compute(
        self, seller_id: int, parts: tuple[object, ...], compute: Callable[[], dict[str, Any]]
    ) -> tuple[dict[str, Any], str]:
        """Returns (payload, how) where how is 'hit', 'coalesced', 'miss' or 'bypass'."""
        try:
            key = self.key(seller_id, *parts)
            raw = self.r.get(key)
            if raw is not None:
                return json.loads(raw), "hit"

            lock = self.r.lock(f"lock:{key}", timeout=self.lock_seconds, blocking=False)
            if lock.acquire(blocking=False):
                try:
                    # Someone may have filled it between our GET and taking the lock.
                    raw = self.r.get(key)
                    if raw is not None:
                        return json.loads(raw), "hit"
                    payload = compute()
                    self.r.set(key, json.dumps(payload, separators=(",", ":")), ex=self.ttl)
                    return payload, "miss"
                finally:
                    try:
                        lock.release()
                    except LockError:  # expired while computing; nothing to release
                        pass

            deadline = time.monotonic() + self.wait_seconds
            while time.monotonic() < deadline:
                time.sleep(0.02)
                raw = self.r.get(key)
                if raw is not None:
                    return json.loads(raw), "coalesced"
            # The leader is slow or died. Compute ourselves rather than fail the request.
            payload = compute()
            self.r.set(key, json.dumps(payload, separators=(",", ":")), ex=self.ttl)
            return payload, "miss"
        except RedisError:
            log.warning("redis unavailable; serving seller %s from the database", seller_id)
            return compute(), "bypass"
