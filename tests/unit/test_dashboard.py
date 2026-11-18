from datetime import date

import pytest

from app.domain.dashboard import aggregate_days, build_dashboard, pct, period_start
from app.domain.economics import DayMetrics, SkuMetrics


def day(d: date, **kw) -> DayMetrics:
    return DayMetrics(day=d, **kw)


def test_period_start_week_is_monday_and_month_is_first():
    assert period_start(date(2026, 11, 18), "week") == date(2026, 11, 16)  # Wednesday
    assert period_start(date(2026, 11, 16), "week") == date(2026, 11, 16)  # Monday itself
    assert period_start(date(2026, 11, 22), "week") == date(2026, 11, 16)  # Sunday
    assert period_start(date(2026, 11, 18), "month") == date(2026, 11, 1)
    assert period_start(date(2026, 11, 18), "day") == date(2026, 11, 18)


def test_week_buckets_cross_month_and_year_boundaries():
    rows = [
        day(date(2025, 12, 31), orders=1, revenue_cents=100),  # Wed
        day(date(2026, 1, 1), orders=1, revenue_cents=50),  # Thu, same ISO week
        day(date(2026, 1, 5), orders=1, revenue_cents=7),  # next Monday
    ]
    buckets = aggregate_days(rows, "week")
    assert [(b.day, b.revenue_cents) for b in buckets] == [
        (date(2025, 12, 29), 150),
        (date(2026, 1, 5), 7),
    ]


@pytest.mark.parametrize(
    ("n", "d", "expected"), [(1, 3, 33.33), (-5, 100, -5.0), (0, 7, 0.0), (5, 0, None)]
)
def test_pct(n, d, expected):
    assert pct(n, d) == expected


def _dash(day_rows, sku_rows, top_n=2):
    return build_dashboard(
        seller_id=1,
        date_from=date(2026, 1, 1),
        date_to=date(2026, 1, 31),
        group_by="day",
        day_rows=day_rows,
        sku_rows=sku_rows,
        products={1: ("A", "home"), 2: ("B", "toys"), 3: ("C", "toys"), 4: ("D", "toys")},
        top_n=top_n,
    )


def test_empty_period_has_null_margin_not_a_division_error():
    d = _dash([], [])
    assert d["totals"]["revenue_cents"] == 0
    assert d["totals"]["margin_pct"] is None
    assert d["totals"]["avg_profit_per_order_cents"] is None
    assert d["series"] == [] and d["top_skus"] == [] and d["worst_skus"] == []


def test_totals_margin_and_series():
    rows = [
        day(date(2026, 1, 1), orders=2, revenue_cents=10_000, cogs_cents=6_000, ad_spend_cents=500),
        day(date(2026, 1, 2), orders=1, revenue_cents=5_000, cogs_cents=4_000),
    ]
    d = _dash(rows, [])
    t = d["totals"]
    assert t["net_profit_cents"] == 10_000 - 6_000 - 500 + 5_000 - 4_000
    assert t["margin_pct"] == round(4_500 * 100 / 15_000, 2)
    assert t["avg_profit_per_order_cents"] == 4_500 // 3
    assert [s["period"] for s in d["series"]] == ["2026-01-01", "2026-01-02"]


def test_best_and_worst_skus_are_ranked_and_deterministic():
    skus = [
        # same product on two days is summed before ranking
        SkuMetrics(1, orders=1, units=1, revenue_cents=1000, cogs_cents=400),
        SkuMetrics(1, orders=1, units=1, revenue_cents=1000, cogs_cents=400),  # A: +1200
        SkuMetrics(2, orders=1, units=1, revenue_cents=1000, cogs_cents=1500),  # B: -500
        SkuMetrics(3, orders=1, units=1, revenue_cents=1000, cogs_cents=1500),  # C: -500 (tie)
        SkuMetrics(4, orders=1, units=1, revenue_cents=1000, cogs_cents=2500),  # D: -1500
        SkuMetrics(9, orders=0, returns_count=2),  # returns only, no sales in range: ignored
    ]
    d = _dash([], skus, top_n=2)
    assert [s["sku"] for s in d["top_skus"]] == ["A", "B"]  # tie B/C broken by product id
    assert [s["sku"] for s in d["worst_skus"]] == ["D", "B"]
    assert d["totals"]["loss_making_skus"] == 3
    assert d["totals"]["active_skus"] == 4
