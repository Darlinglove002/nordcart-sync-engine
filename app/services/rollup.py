"""Reading and rebuilding the per-day rollups, and computing a dashboard from either source."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

from sqlalchemy import BigInteger, CursorResult, delete, func, insert, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    AdSpend,
    DailySellerMetrics,
    DailySkuMetrics,
    Order,
    Product,
    Return,
    Seller,
)
from app.domain.dashboard import GroupBy, build_dashboard
from app.domain.economics import METRIC_FIELDS, SKU_SUMMED_FIELDS, DayMetrics, SkuMetrics
from app.services.sql import DAILY_SQL, SKU_DAILY_SQL

log = logging.getLogger(__name__)


def utc_today() -> date:
    return datetime.now(UTC).date()


# --------------------------------------------------------------------------- live (raw tables)


def fetch_daily_live(s: Session, seller_id: int, d1: date, d2: date) -> list[DayMetrics]:
    rows = s.execute(DAILY_SQL, {"seller_id": seller_id, "date_from": d1, "date_to": d2})
    return [DayMetrics(**dict(r._mapping)) for r in rows]


def fetch_sku_daily_live(
    s: Session, seller_id: int, d1: date, d2: date
) -> list[tuple[date, SkuMetrics]]:
    rows = s.execute(SKU_DAILY_SQL, {"seller_id": seller_id, "date_from": d1, "date_to": d2})
    out = []
    for r in rows:
        m = dict(r._mapping)
        day = m.pop("day")
        out.append((day, SkuMetrics(**m)))
    return out


# ------------------------------------------------------------------------ rollup (small tables)


def fetch_daily_rollup(s: Session, seller_id: int, d1: date, d2: date) -> list[DayMetrics]:
    cols = [DailySellerMetrics.day, *(getattr(DailySellerMetrics, f) for f in METRIC_FIELDS)]
    rows = s.execute(
        select(*cols)
        .where(DailySellerMetrics.seller_id == seller_id, DailySellerMetrics.day.between(d1, d2))
        .order_by(DailySellerMetrics.day)
    )
    return [DayMetrics(**dict(r._mapping)) for r in rows]


def fetch_sku_rollup(s: Session, seller_id: int, d1: date, d2: date) -> list[SkuMetrics]:
    # Summed in SQL: ~200 rows come back instead of one row per SKU-day (10k+ per seller).
    cols = [
        func.sum(getattr(DailySkuMetrics, f)).cast(BigInteger).label(f) for f in SKU_SUMMED_FIELDS
    ]
    rows = s.execute(
        select(DailySkuMetrics.product_id, *cols)
        .where(DailySkuMetrics.seller_id == seller_id, DailySkuMetrics.day.between(d1, d2))
        .group_by(DailySkuMetrics.product_id)
    )
    return [SkuMetrics(**dict(r._mapping)) for r in rows]


def product_labels(s: Session, seller_id: int) -> dict[int, tuple[str, str]]:
    rows = s.execute(
        select(Product.id, Product.sku, Product.category).where(Product.seller_id == seller_id)
    )
    return {pid: (sku, cat) for pid, sku, cat in rows}


@dataclass(slots=True)
class RollupState:
    ready: bool
    stale: bool
    last_recalculated_at: datetime | None


def rollup_state(s: Session, seller_id: int) -> RollupState | None:
    row = s.execute(
        select(Seller.last_recalculated_at, Seller.dirty_since).where(Seller.id == seller_id)
    ).one_or_none()
    if row is None:
        return None
    last, dirty = row
    return RollupState(ready=last is not None, stale=dirty is not None, last_recalculated_at=last)


def compute_dashboard(
    s: Session,
    seller_id: int,
    d1: date,
    d2: date,
    group_by: GroupBy,
    top_n: int,
    *,
    source: str,
) -> dict[str, Any]:
    """`source` is 'rollup' or 'live'. Same output shape and (by construction) same numbers."""
    if source == "live":
        day_rows = fetch_daily_live(s, seller_id, d1, d2)
        sku_rows = [m for _, m in fetch_sku_daily_live(s, seller_id, d1, d2)]
    else:
        day_rows = fetch_daily_rollup(s, seller_id, d1, d2)
        sku_rows = fetch_sku_rollup(s, seller_id, d1, d2)

    payload = build_dashboard(
        seller_id=seller_id,
        date_from=d1,
        date_to=d2,
        group_by=group_by,
        day_rows=day_rows,
        sku_rows=sku_rows,
        products=product_labels(s, seller_id),
        top_n=top_n,
    )
    state = rollup_state(s, seller_id)
    payload["meta"] = {
        "source": source,
        "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rollup_updated_at": (
            state.last_recalculated_at.isoformat(timespec="seconds")
            if state and state.last_recalculated_at
            else None
        ),
        # True when new orders were ingested that the rollup has not absorbed yet.
        "stale": bool(state and state.stale) if source == "rollup" else False,
    }
    return payload


# ------------------------------------------------------------------------------- recalculation


@dataclass(slots=True)
class RecalcResult:
    seller_id: int
    date_from: date | None
    date_to: date | None
    days: int
    sku_rows: int
    cleared_dirty: bool


def data_bounds(s: Session, seller_id: int) -> tuple[date, date] | None:
    lo_o, hi_o = s.execute(
        select(func.min(Order.day), func.max(Order.day)).where(Order.seller_id == seller_id)
    ).one()
    lo_r, hi_r = s.execute(
        select(func.min(Return.day), func.max(Return.day)).where(Return.seller_id == seller_id)
    ).one()
    lo_a, hi_a = s.execute(
        select(func.min(AdSpend.day), func.max(AdSpend.day)).where(AdSpend.seller_id == seller_id)
    ).one()
    los = [d for d in (lo_o, lo_r, lo_a) if d]
    his = [d for d in (hi_o, hi_r, hi_a) if d]
    if not los:
        return None
    return min(los), max(his)


def recalculate(s: Session, seller_id: int, *, full: bool = False) -> RecalcResult | None:
    """Rebuild rollups for the days that may have changed. One transaction; caller commits.

    Incremental by default: only days >= `dirty_since` are recomputed. `full=True` (or a
    seller that was never calculated) rebuilds everything. Returns None for an unknown seller.
    """
    seller = s.execute(
        select(Seller.data_version, Seller.dirty_since, Seller.last_recalculated_at).where(
            Seller.id == seller_id
        )
    ).one_or_none()
    if seller is None:
        return None
    seen_version, dirty_since, last_calc = seller

    bounds = data_bounds(s, seller_id)
    if bounds is None:
        s.execute(
            update(Seller).where(Seller.id == seller_id).values(last_recalculated_at=func.now())
        )
        return RecalcResult(seller_id, None, None, 0, 0, cleared_dirty=False)

    lo, hi = bounds
    if full or last_calc is None or dirty_since is None:
        start = lo
    else:
        start = max(lo, dirty_since)
    # Returns and ad spend can land after the last order, so use the true upper bound.
    end = max(hi, start)

    day_rows = fetch_daily_live(s, seller_id, start, end)
    sku_rows = fetch_sku_daily_live(s, seller_id, start, end)

    s.execute(
        delete(DailySellerMetrics).where(
            DailySellerMetrics.seller_id == seller_id, DailySellerMetrics.day.between(start, end)
        )
    )
    s.execute(
        delete(DailySkuMetrics).where(
            DailySkuMetrics.seller_id == seller_id, DailySkuMetrics.day.between(start, end)
        )
    )
    if day_rows:
        s.execute(
            insert(DailySellerMetrics),
            [
                {
                    "seller_id": seller_id,
                    **{f: getattr(m, f) for f in METRIC_FIELDS},
                    "day": m.day,
                    "net_profit_cents": m.net_profit_cents,
                }
                for m in day_rows
            ],
        )
    if sku_rows:
        s.execute(
            insert(DailySkuMetrics),
            [
                {
                    "seller_id": seller_id,
                    "day": day,
                    "product_id": m.product_id,
                    **{f: getattr(m, f) for f in SKU_SUMMED_FIELDS},
                    "net_profit_cents": m.net_profit_cents,
                }
                for day, m in sku_rows
            ],
        )

    # Compare-and-set: if an ingest bumped data_version while we were computing, leave the
    # seller dirty so the next run picks the new rows up.
    cleared = cast(
        "CursorResult[Any]",
        s.execute(
            update(Seller)
            .where(Seller.id == seller_id, Seller.data_version == seen_version)
            .values(dirty_since=None, last_recalculated_at=func.now())
        ),
    ).rowcount
    if not cleared:
        s.execute(
            update(Seller).where(Seller.id == seller_id).values(last_recalculated_at=func.now())
        )
    return RecalcResult(seller_id, start, end, len(day_rows), len(sku_rows), bool(cleared))
