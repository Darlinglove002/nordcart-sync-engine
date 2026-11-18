from __future__ import annotations

import threading
import time

import redis

from app.cache import DashboardCache, purge_all
from tests.conftest import TEST_REDIS_URL


def make(r) -> DashboardCache:
    return DashboardCache(r, ttl=60, lock_seconds=10, wait_seconds=3.0)


def test_miss_then_hit_and_payload_roundtrips(r):
    c = make(r)
    calls = []

    def compute():
        calls.append(1)
        return {"n": len(calls), "nested": {"a": [1, 2, None]}}

    first, how1 = c.get_or_compute(1, ("2026-01-01", "day"), compute)
    second, how2 = c.get_or_compute(1, ("2026-01-01", "day"), compute)
    assert (how1, how2) == ("miss", "hit")
    assert first == second == {"n": 1, "nested": {"a": [1, 2, None]}}
    assert len(calls) == 1


def test_different_parameters_and_sellers_do_not_share_entries(r):
    c = make(r)
    a, _ = c.get_or_compute(1, ("x",), lambda: {"v": "seller1-x"})
    b, how_b = c.get_or_compute(1, ("y",), lambda: {"v": "seller1-y"})
    other, how_o = c.get_or_compute(2, ("x",), lambda: {"v": "seller2-x"})
    assert (a["v"], b["v"], other["v"]) == ("seller1-x", "seller1-y", "seller2-x")
    assert how_b == how_o == "miss"


def test_invalidate_is_one_atomic_step_and_scoped_to_one_seller(r):
    c = make(r)
    c.get_or_compute(1, ("x",), lambda: {"v": 1})
    c.get_or_compute(2, ("x",), lambda: {"v": 1})
    c.invalidate(1)
    again, how1 = c.get_or_compute(1, ("x",), lambda: {"v": 2})
    kept, how2 = c.get_or_compute(2, ("x",), lambda: {"v": 99})
    assert (how1, again["v"]) == ("miss", 2)
    assert (how2, kept["v"]) == ("hit", 1)  # seller 2 untouched


def test_entries_expire_by_ttl(r):
    c = DashboardCache(r, ttl=1, lock_seconds=10)
    c.get_or_compute(1, ("x",), lambda: {"v": 1})
    time.sleep(1.2)
    _, how = c.get_or_compute(1, ("x",), lambda: {"v": 2})
    assert how == "miss"


def test_single_flight_many_concurrent_misses_compute_once(r):
    c = make(r)
    calls = []
    results, hows = [], []
    start = threading.Barrier(12)

    def slow():
        calls.append(1)
        time.sleep(0.4)
        return {"v": "computed"}

    def worker():
        start.wait()
        p, how = c.get_or_compute(1, ("hot",), slow)
        results.append(p)
        hows.append(how)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]

    assert len(calls) == 1  # one query, not twelve
    assert all(p == {"v": "computed"} for p in results) and len(results) == 12
    assert hows.count("miss") == 1 and set(hows) <= {"miss", "coalesced", "hit"}


def test_dead_leader_does_not_hang_followers(r):
    """If the lock holder never publishes a result, a waiter computes for itself."""
    c = DashboardCache(r, ttl=60, lock_seconds=10, wait_seconds=0.3)
    key = c.key(1, "stuck")
    r.set(f"lock:{key}", "someone-else", ex=10)  # a leader that will never finish
    t0 = time.monotonic()
    payload, how = c.get_or_compute(1, ("stuck",), lambda: {"v": "fallback"})
    assert payload == {"v": "fallback"} and how == "miss"
    assert time.monotonic() - t0 < 2


def test_fails_open_when_redis_is_down():
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    c = DashboardCache(dead, ttl=60, lock_seconds=10)
    payload, how = c.get_or_compute(1, ("x",), lambda: {"v": "from-db"})
    assert (payload, how) == ({"v": "from-db"}, "bypass")
    c.invalidate(1)  # must not raise


def test_purge_all_clears_dashboard_auth_and_lock_keys(r):
    r.set("ue:1:v0:x", "1")
    r.set("auth:abc", "1")
    r.set("lock:ue:1", "1")
    r.set("unrelated", "keep")
    assert purge_all(r) == 3
    assert r.get("unrelated") == "keep"
    assert redis.Redis.from_url(TEST_REDIS_URL).get("ue:1:v0:x") is None
