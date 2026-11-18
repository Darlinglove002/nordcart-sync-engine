"""CLI: python -m app.generator --orders 50000 --sellers 5 [--reset]

Generates the dataset, then builds the rollups in-process (no broker needed) and prints the
per-seller API keys. The keys are shown once; only their hashes are stored.
"""

import argparse
import time

from redis.exceptions import RedisError

from app.cache import DashboardCache, get_redis, purge_all
from app.config import get_settings
from app.db.session import get_engine, session_scope
from app.generator.synthetic import generate, reset_all
from app.services.rollup import recalculate


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--sellers", type=int, default=5)
    p.add_argument("--orders", type=int, default=50_000)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--reset", action="store_true", help="wipe all existing data first")
    p.add_argument("--no-recalc", action="store_true", help="skip building rollups")
    a = p.parse_args()

    engine = get_engine()
    cfg = get_settings()
    cache = DashboardCache(get_redis(), cfg.cache_ttl_seconds, cfg.cache_lock_seconds)
    if a.reset:
        reset_all(engine)
        # Seller ids restart from 1, so any cached payload would belong to a different seller.
        try:
            purge_all(get_redis())
        except RedisError:
            print("warning: Redis unreachable, cached dashboards will expire by TTL")
        print("existing data wiped")

    t0 = time.perf_counter()
    res = generate(engine, sellers=a.sellers, orders=a.orders, days=a.days, seed=a.seed)
    print(
        f"generated {res.orders:,} orders, {res.returns:,} returns, {res.products:,} products, "
        f"{res.ad_days:,} ad-spend days in {time.perf_counter() - t0:.1f}s"
    )

    if not a.no_recalc:
        for s in res.sellers:
            t = time.perf_counter()
            with session_scope() as session:
                r = recalculate(session, s.id, full=True)
            cache.invalidate(s.id)
            print(
                f"  rollup seller {s.id}: {r.days} days, {r.sku_rows:,} sku-day rows "
                f"in {time.perf_counter() - t:.2f}s"
            )

    print("\nseller API keys (shown once):")
    for s in res.sellers:
        print(f"  seller {s.id:>3}  {s.name:<32} {s.api_key}")


if __name__ == "__main__":
    main()
