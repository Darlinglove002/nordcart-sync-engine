"""Turns per-day / per-SKU metrics into the dashboard payload. Pure functions, no I/O."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any, Literal

from app.domain.economics import METRIC_FIELDS, DayMetrics, SkuMetrics

GroupBy = Literal["day", "week", "month"]


def period_start(d: date, group_by: GroupBy) -> date:
    if group_by == "day":
        return d
    if group_by == "week":  # ISO week, Monday
        return d - timedelta(days=d.weekday())
    return d.replace(day=1)


def pct(numerator: int, denominator: int) -> float | None:
    """Percentage with 2 decimals; None when there is nothing to divide by."""
    if denominator == 0:
        return None
    return round(numerator * 100 / denominator, 2)


def _metrics_dict(m: DayMetrics) -> dict[str, Any]:
    d = {f: getattr(m, f) for f in METRIC_FIELDS}
    d["net_profit_cents"] = m.net_profit_cents
    d["margin_pct"] = pct(m.net_profit_cents, m.revenue_cents)
    d["return_rate_pct"] = pct(m.returns_count, m.orders)
    return d


def aggregate_days(rows: Iterable[DayMetrics], group_by: GroupBy) -> list[DayMetrics]:
    buckets: dict[date, DayMetrics] = {}
    for r in rows:
        key = period_start(r.day, group_by)
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = DayMetrics(day=key)
        b.add(r)
    return [buckets[k] for k in sorted(buckets)]


def aggregate_skus(rows: Iterable[SkuMetrics]) -> dict[int, SkuMetrics]:
    out: dict[int, SkuMetrics] = {}
    for r in rows:
        acc = out.get(r.product_id)
        if acc is None:
            acc = out[r.product_id] = SkuMetrics(product_id=r.product_id)
        acc.add(r)
    return out


def _sku_dict(s: SkuMetrics, products: dict[int, tuple[str, str]]) -> dict[str, Any]:
    sku, category = products.get(s.product_id, (f"#{s.product_id}", "unknown"))
    return {
        "sku": sku,
        "category": category,
        "units": s.units,
        "revenue_cents": s.revenue_cents,
        "net_profit_cents": s.net_profit_cents,
        "margin_pct": pct(s.net_profit_cents, s.revenue_cents),
        "return_rate_pct": pct(s.returns_count, s.orders),
    }


def build_dashboard(
    *,
    seller_id: int,
    date_from: date,
    date_to: date,
    group_by: GroupBy,
    day_rows: list[DayMetrics],
    sku_rows: list[SkuMetrics],
    products: dict[int, tuple[str, str]],
    top_n: int,
) -> dict[str, Any]:
    total = DayMetrics(day=date_from)
    for r in day_rows:
        total.add(r)

    series = []
    for b in aggregate_days(day_rows, group_by):
        item = _metrics_dict(b)
        item["period"] = b.day.isoformat()
        series.append(
            {
                k: item[k]
                for k in (
                    "period",
                    "orders",
                    "revenue_cents",
                    "net_profit_cents",
                    "margin_pct",
                    "returns_count",
                )
            }
        )

    skus = [s for s in aggregate_skus(sku_rows).values() if s.orders > 0]
    # Deterministic order: ties broken by product id.
    best = sorted(skus, key=lambda s: (-s.net_profit_cents, s.product_id))[:top_n]
    worst = sorted(
        (s for s in skus if s.net_profit_cents < 0),
        key=lambda s: (s.net_profit_cents, s.product_id),
    )[:top_n]

    totals = _metrics_dict(total)
    totals["avg_profit_per_order_cents"] = (
        total.net_profit_cents // total.orders if total.orders else None
    )
    totals["loss_making_skus"] = sum(1 for s in skus if s.net_profit_cents < 0)
    totals["active_skus"] = len(skus)

    return {
        "seller_id": seller_id,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "group_by": group_by,
        "totals": totals,
        "series": series,
        "top_skus": [_sku_dict(s, products) for s in best],
        "worst_skus": [_sku_dict(s, products) for s in worst],
    }
