"""The SQL aggregation (what production runs) against the plain-Python rules (what a human reads)."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

import pytest

from app.db.session import session_scope
from app.domain.economics import METRIC_FIELDS, reference_daily
from app.services.rollup import fetch_daily_live, fetch_sku_daily_live
from tests.helpers import build_dataset


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_daily_sql_equals_reference_for_every_day_and_field(engine, seed):
    ds = build_dataset(engine, seed=seed)
    expected = reference_daily(
        ds.orders, ds.returns, ds.unit_cost, ds.ad_spend, ds.first_day, ds.last_day
    )
    with session_scope() as s:
        got = {m.day: m for m in fetch_daily_live(s, ds.seller_id, ds.first_day, ds.last_day)}

    assert got.keys() == expected.keys()
    for day, exp in expected.items():
        for f in METRIC_FIELDS:
            assert getattr(got[day], f) == getattr(exp, f), f"{day} {f}"
        assert got[day].net_profit_cents == exp.net_profit_cents


def test_sub_range_only_counts_days_inside_it(engine):
    ds = build_dataset(engine, seed=4)
    lo, hi = ds.first_day + timedelta(days=10), ds.first_day + timedelta(days=20)
    expected = reference_daily(ds.orders, ds.returns, ds.unit_cost, ds.ad_spend, lo, hi)
    with session_scope() as s:
        got = {m.day: m for m in fetch_daily_live(s, ds.seller_id, lo, hi)}
    assert got.keys() == expected.keys()
    assert all(lo <= d <= hi for d in got)
    assert sum(m.net_profit_cents for m in got.values()) == sum(
        m.net_profit_cents for m in expected.values()
    )


def test_huge_basket_does_not_overflow_int32(engine):
    ds = build_dataset(engine, seed=5)
    with session_scope() as s:
        day = {m.day: m for m in fetch_daily_live(s, ds.seller_id, ds.first_day, ds.first_day)}
    assert day[ds.first_day].revenue_cents >= 100 * 19_000_000


def test_sku_level_sums_back_to_the_seller_level_without_ads(engine):
    ds = build_dataset(engine, seed=6)
    with session_scope() as s:
        daily = fetch_daily_live(s, ds.seller_id, ds.first_day, ds.last_day)
        sku = fetch_sku_daily_live(s, ds.seller_id, ds.first_day, ds.last_day)
    by_field = defaultdict(int)
    for _, m in sku:
        for f in (
            "orders",
            "units",
            "revenue_cents",
            "cogs_cents",
            "refunds_cents",
            "returns_count",
        ):
            by_field[f] += getattr(m, f)
        by_field["net"] += m.net_profit_cents
    for f in ("orders", "units", "revenue_cents", "cogs_cents", "refunds_cents", "returns_count"):
        assert by_field[f] == sum(getattr(d, f) for d in daily), f
    # SKU profit excludes ads by design: the difference is exactly the ad spend.
    assert by_field["net"] == sum(d.net_profit_cents + d.ad_spend_cents for d in daily)


def test_other_sellers_data_never_leaks_into_a_seller(engine):
    a = build_dataset(engine, seed=8, name="A")
    b = build_dataset(engine, seed=9, name="B", id_offset=10_000)
    with session_scope() as s:
        rows_a = fetch_daily_live(s, a.seller_id, a.first_day, a.last_day)
    exp = reference_daily(a.orders, a.returns, a.unit_cost, a.ad_spend, a.first_day, a.last_day)
    assert sum(m.revenue_cents for m in rows_a) == sum(m.revenue_cents for m in exp.values())
    assert a.seller_id != b.seller_id
