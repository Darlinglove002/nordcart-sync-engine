"""Background jobs. Anything that touches more rows than a request should wait for lives here."""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from app.cache import DashboardCache, get_redis
from app.config import get_settings
from app.db.models import Seller
from app.db.session import get_engine, session_scope
from app.generator.synthetic import generate
from app.services.rollup import recalculate
from app.worker.celery_app import celery_app

log = logging.getLogger(__name__)


def _cache() -> DashboardCache:
    s = get_settings()
    return DashboardCache(get_redis(), s.cache_ttl_seconds, s.cache_lock_seconds)


@celery_app.task(name="app.worker.tasks.recalculate_seller")
def recalculate_seller(seller_id: int, full: bool = False) -> dict[str, Any]:
    """Rebuild a seller's rollups, then invalidate their cached dashboards.

    One recalculation per seller at a time (Redis lock). If one is already running the new
    request is dropped: the seller stays dirty, so the periodic sweep picks the data up.
    """
    redis_client = get_redis()
    lock = redis_client.lock(f"lock:recalc:{seller_id}", timeout=1200, blocking=False)
    if not lock.acquire(blocking=False):
        log.info("seller %s: recalculation already running, skipped", seller_id)
        return {"seller_id": seller_id, "status": "skipped", "reason": "already running"}
    try:
        started = time.perf_counter()
        with session_scope() as session:
            result = recalculate(session, seller_id, full=full)
        if result is None:
            log.warning("seller %s: not found, nothing to recalculate", seller_id)
            return {"seller_id": seller_id, "status": "not_found"}
        # After the commit: a reader can never cache pre-recalculation data under the new version.
        _cache().invalidate(seller_id)
        seconds = round(time.perf_counter() - started, 3)
        log.info(
            "seller %s: recalculated %d days, %d sku rows in %.2fs (full=%s)",
            seller_id,
            result.days,
            result.sku_rows,
            seconds,
            full,
        )
        return {
            "seller_id": seller_id,
            "status": "done",
            "date_from": result.date_from.isoformat() if result.date_from else None,
            "date_to": result.date_to.isoformat() if result.date_to else None,
            "days": result.days,
            "sku_rows": result.sku_rows,
            "cleared_dirty": result.cleared_dirty,
            "seconds": seconds,
        }
    finally:
        try:
            lock.release()
        except Exception:  # lock expired; nothing to release
            log.warning("recalc lock for seller %s expired before release", seller_id)


@celery_app.task(name="app.worker.tasks.recalculate_dirty")
def recalculate_dirty() -> dict[str, Any]:
    """Beat sweep: queue a recalculation for every seller with unprocessed data."""
    with session_scope() as session:
        ids = list(
            session.execute(select(Seller.id).where(Seller.dirty_since.is_not(None))).scalars()
        )
    for sid in ids:
        recalculate_seller.delay(sid)
    # The sweep runs every minute: log only when it found something to do.
    log.log(logging.INFO if ids else logging.DEBUG, "sweep: queued %d dirty sellers", len(ids))
    return {"queued": ids}


@celery_app.task(name="app.worker.tasks.generate_dataset")
def generate_dataset(sellers: int, orders: int, days: int, seed: int) -> dict[str, Any]:
    """Generate synthetic data, then queue a full rollup for each new seller.

    API keys are NOT returned here: task results sit in the result backend. Read them from the
    CLI (`python -m app.generator`) or rotate keys through the admin API.
    """
    started = time.perf_counter()
    res = generate(get_engine(), sellers=sellers, orders=orders, days=days, seed=seed)
    for s in res.sellers:
        recalculate_seller.delay(s.id, True)
    log.info(
        "generated %d orders for %d sellers in %.1fs",
        res.orders,
        len(res.sellers),
        time.perf_counter() - started,
    )
    return {
        "seller_ids": [s.id for s in res.sellers],
        "orders": res.orders,
        "returns": res.returns,
        "products": res.products,
        "seconds": round(time.perf_counter() - started, 3),
    }
