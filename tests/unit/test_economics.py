from datetime import date

import pytest

from app.domain.economics import (
    DayMetrics,
    OrderRow,
    ReturnRow,
    apply_bps,
    reference_daily,
)


@pytest.mark.parametrize(
    ("amount", "bps", "expected"),
    [
        (10_000, 1200, 1200),  # exact
        (1, 5000, 1),  # 0.5 rounds up
        (3, 1500, 0),  # 0.45 rounds down
        (999, 1000, 100),  # 99.9
        (0, 1500, 0),
        (5_000_000_000, 1500, 750_000_000),  # beyond int32
    ],
)
def test_apply_bps_rounds_half_up(amount, bps, expected):
    assert apply_bps(amount, bps) == expected


def test_net_profit_uses_every_component():
    m = DayMetrics(
        day=date(2026, 1, 1),
        revenue_cents=100_000,
        cogs_cents=40_000,
        commission_cents=12_000,
        logistics_cents=5_000,
        ad_spend_cents=3_000,
        refunds_cents=10_000,
        commission_refund_cents=1_200,
        return_logistics_cents=700,
        restocked_cogs_cents=4_000,
    )
    # 100000 - 40000 - 12000 - 5000 - 3000 - 10000 + 1200 - 700 + 4000
    assert m.net_profit_cents == 34_500


def test_add_sums_fields_and_keeps_day():
    a = DayMetrics(day=date(2026, 1, 1), orders=1, revenue_cents=10)
    a.add(DayMetrics(day=date(2026, 1, 2), orders=2, revenue_cents=5, ad_spend_cents=3))
    assert (a.day, a.orders, a.revenue_cents, a.ad_spend_cents) == (date(2026, 1, 1), 3, 15, 3)


D1, D2, D3 = date(2026, 3, 1), date(2026, 3, 2), date(2026, 3, 4)


def _hand_dataset():
    orders = [
        # 2 units at 50.00, 10.00 discount -> revenue 90.00, cogs 2*30=60.00, commission 12% = 10.80
        OrderRow(1, 7, D1, 2, 5000, 1000, 1200, 700, "delivered"),
        # cancelled: contributes nothing, neither does its return
        OrderRow(2, 7, D1, 1, 9999, 0, 1200, 700, "cancelled"),
        OrderRow(3, 8, D2, 1, 2000, 0, 1500, 300, "delivered"),
    ]
    returns = [
        # one of the two units of order 1 comes back on D3, restockable
        ReturnRow(1, D3, 1, 4500, 400, True),
        # a return attached to the cancelled order must be ignored
        ReturnRow(2, D3, 1, 9999, 999, True),
    ]
    return orders, returns


def test_reference_daily_hand_computed():
    orders, returns = _hand_dataset()
    out = reference_daily(orders, returns, {7: 3000, 8: 800}, {D1: 500, D3: 250}, D1, D3)

    d1 = out[D1]
    assert (d1.orders, d1.units) == (1, 2)
    assert d1.revenue_cents == 9000
    assert d1.cogs_cents == 6000
    assert d1.commission_cents == 1080
    assert d1.logistics_cents == 700
    assert d1.ad_spend_cents == 500
    assert d1.net_profit_cents == 9000 - 6000 - 1080 - 700 - 500

    d3 = out[D3]
    assert d3.orders == 0 and d3.returns_count == 1  # only the real return counts
    assert d3.refunds_cents == 4500
    assert d3.commission_refund_cents == 540  # 12% of 45.00
    assert d3.return_logistics_cents == 400
    assert d3.restocked_cogs_cents == 3000  # one unit back on the shelf
    assert d3.ad_spend_cents == 250
    assert d3.net_profit_cents == -4500 + 540 - 400 + 3000 - 250


def test_reference_daily_respects_the_date_range():
    orders, returns = _hand_dataset()
    out = reference_daily(orders, returns, {7: 3000, 8: 800}, {}, D2, D2)
    assert list(out) == [D2]
    assert out[D2].revenue_cents == 2000


def test_non_restockable_return_keeps_the_cogs_written_off():
    orders = [OrderRow(1, 7, D1, 1, 5000, 0, 1000, 0, "delivered")]
    returns = [ReturnRow(1, D2, 1, 5000, 0, False)]
    out = reference_daily(orders, returns, {7: 3000}, {}, D1, D2)
    assert out[D2].restocked_cogs_cents == 0
    total = sum(m.net_profit_cents for m in out.values())
    # revenue 5000 - cogs 3000 - commission 500 - refund 5000 + commission back 500
    assert total == 5000 - 3000 - 500 - 5000 + 500
