"""CLI: python -m app.generator --orders 50000 --sellers 5 [--reset]

Generates the dataset, then builds the rollups in-process (no broker needed) and prints the
per-seller API keys. The keys are shown once; only their hashes are stored.

Progress goes through logging (stderr). The keys are the one thing this CLI exists to show and
they are secrets, so they are written to stdout for the person at the terminal and never
through logging, whose output may be shipped to a log store.
"""

import argparse
import logging
import time

from redis.exceptions import RedisError

from app.cache import DashboardCache, get_redis, purge_all
from app.config import get_settings
from app.db.session import get_engine, session_scope
from app.generator.synthetic import generate, reset_all
from app.logging_config import configure_logging
from app.services.rollup import recalculate

log = logging.getLogger("app.generator")  # __name__ would be "__main__" under `python -m`


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--sellers", type=int, default=5)
    p.add_argument("--orders", type=int, default=50_000)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--reset", action="store_true", help="wipe all existing data first")
    p.add_argument("--no-recalc", action="store_true", help="skip building rollups")
    a = p.parse_args()
    configure_logging()

    engine = get_engine()
    cfg = get_settings()
    cache = DashboardCache(get_redis(), cfg.cache_ttl_seconds, cfg.cache_lock_seconds)
    if a.reset:
        reset_all(engine)
        # Seller ids restart from 1, so any cached payload would belong to a different seller.
        try:
            purge_all(get_redis())
        except RedisError:
            log.warning("Redis unreachable, cached dashboards will expire by TTL")
        log.info("existing data wiped")

    t0 = time.perf_counter()
    res = generate(engine, sellers=a.sellers, orders=a.orders, days=a.days, seed=a.seed)
    log.info(
        "generated %s orders, %s returns, %s products, %s ad-spend days in %.1fs",
        f"{res.orders:,}",
        f"{res.returns:,}",
        f"{res.products:,}",
        f"{res.ad_days:,}",
        time.perf_counter() - t0,
    )

    if not a.no_recalc:
        for s in res.sellers:
            t = time.perf_counter()
            with session_scope() as session:
                r = recalculate(session, s.id, full=True)
            cache.invalidate(s.id)
            if r is None:  # cannot happen right after generate(); keeps the type honest
                log.warning("rollup seller %s: seller not found", s.id)
                continue
            log.info(
                "rollup seller %s: %d days, %s sku-day rows in %.2fs",
                s.id,
                r.days,
                f"{r.sku_rows:,}",
                time.perf_counter() - t,
            )

    # Secrets: stdout only, never logged (see the module docstring).
    print("\nseller API keys (shown once):")  # noqa: T201
    for s in res.sellers:
        print(f"  seller {s.id:>3}  {s.name:<32} {s.api_key}")  # noqa: T201


if __name__ == "__main__":
    main()
