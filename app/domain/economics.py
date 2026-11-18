"""Unit-economics rules for a marketplace seller.

All money is integer cents. There are no floats anywhere in the arithmetic: a margin that
is off by a cent after 50 000 orders is a bug a seller will notice.

Rules (one delivered order line, one return):

    revenue      = quantity * unit_price - discount
    cogs         = quantity * unit_cost
    commission   = round_half_up(revenue * commission_bps / 10_000)
    logistics    = outbound delivery cost charged by the marketplace

    a return, booked on the day it arrives:
      - refunds the customer: refund
      - the marketplace gives back its commission on the refunded amount
      - the seller pays return logistics
      - if the goods are restockable, their cost comes back (cogs recovered), otherwise it
        stays written off

    net_profit = revenue - cogs - commission - logistics - ad_spend
                 - refunds + commission_refund - return_logistics + restocked_cogs

Cancelled orders do not count at all. Ad spend is a seller-level cost (it cannot be
attributed to a SKU), so SKU-level profit excludes it.

The SQL in `app.services.sql` implements the same rules over millions of rows; this module
is the readable reference, and the tests check the two agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

BPS = 10_000


def apply_bps(amount_cents: int, bps: int) -> int:
    """amount * bps / 10_000, rounded half up. Matches `(amount * bps + 5000) / 10000` in SQL."""
    return (amount_cents * bps + BPS // 2) // BPS


@dataclass(slots=True)
class DayMetrics:
    """Everything the dashboard needs for one seller-day (or one SKU-day, ad_spend = 0)."""

    day: date
    orders: int = 0
    units: int = 0
    revenue_cents: int = 0
    cogs_cents: int = 0
    commission_cents: int = 0
    logistics_cents: int = 0
    ad_spend_cents: int = 0
    returns_count: int = 0
    refunds_cents: int = 0
    commission_refund_cents: int = 0
    return_logistics_cents: int = 0
    restocked_cogs_cents: int = 0

    @property
    def net_profit_cents(self) -> int:
        return (
            self.revenue_cents
            - self.cogs_cents
            - self.commission_cents
            - self.logistics_cents
            - self.ad_spend_cents
            - self.refunds_cents
            + self.commission_refund_cents
            - self.return_logistics_cents
            + self.restocked_cogs_cents
        )

    def add(self, other: DayMetrics) -> None:
        for f in _SUMMED_FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))


_SUMMED_FIELDS = (
    "orders",
    "units",
    "revenue_cents",
    "cogs_cents",
    "commission_cents",
    "logistics_cents",
    "ad_spend_cents",
    "returns_count",
    "refunds_cents",
    "commission_refund_cents",
    "return_logistics_cents",
    "restocked_cogs_cents",
)

METRIC_FIELDS = _SUMMED_FIELDS


@dataclass(slots=True)
class SkuMetrics:
    product_id: int
    units: int = 0
    revenue_cents: int = 0
    cogs_cents: int = 0
    commission_cents: int = 0
    logistics_cents: int = 0
    refunds_cents: int = 0
    commission_refund_cents: int = 0
    return_logistics_cents: int = 0
    restocked_cogs_cents: int = 0
    returns_count: int = 0
    orders: int = 0

    @property
    def net_profit_cents(self) -> int:
        return (
            self.revenue_cents
            - self.cogs_cents
            - self.commission_cents
            - self.logistics_cents
            - self.refunds_cents
            + self.commission_refund_cents
            - self.return_logistics_cents
            + self.restocked_cogs_cents
        )

    def add(self, other: SkuMetrics) -> None:
        for f in SKU_SUMMED_FIELDS:
            setattr(self, f, getattr(self, f) + getattr(other, f))


SKU_SUMMED_FIELDS = (
    "orders",
    "units",
    "revenue_cents",
    "cogs_cents",
    "commission_cents",
    "logistics_cents",
    "returns_count",
    "refunds_cents",
    "commission_refund_cents",
    "return_logistics_cents",
    "restocked_cogs_cents",
)


# ---------------------------------------------------------------------------------------
# Reference implementation over plain Python objects. Used by tests as the oracle for the SQL.
# ---------------------------------------------------------------------------------------


@dataclass(slots=True)
class OrderRow:
    id: int
    product_id: int
    day: date
    quantity: int
    unit_price_cents: int
    discount_cents: int
    commission_bps: int
    logistics_cents: int
    status: str


@dataclass(slots=True)
class ReturnRow:
    order_id: int
    day: date
    quantity: int
    refund_cents: int
    return_logistics_cents: int
    restockable: bool


def reference_daily(
    orders: list[OrderRow],
    returns: list[ReturnRow],
    unit_cost: dict[int, int],
    ad_spend: dict[date, int],
    date_from: date,
    date_to: date,
) -> dict[date, DayMetrics]:
    by_id = {o.id: o for o in orders}
    out: dict[date, DayMetrics] = {}

    def slot(d: date) -> DayMetrics:
        return out.setdefault(d, DayMetrics(day=d))

    for o in orders:
        if o.status != "delivered" or not (date_from <= o.day <= date_to):
            continue
        m = slot(o.day)
        revenue = o.quantity * o.unit_price_cents - o.discount_cents
        m.orders += 1
        m.units += o.quantity
        m.revenue_cents += revenue
        m.cogs_cents += o.quantity * unit_cost[o.product_id]
        m.commission_cents += apply_bps(revenue, o.commission_bps)
        m.logistics_cents += o.logistics_cents

    for r in returns:
        o = by_id[r.order_id]
        if o.status != "delivered" or not (date_from <= r.day <= date_to):
            continue
        m = slot(r.day)
        m.returns_count += 1
        m.refunds_cents += r.refund_cents
        m.commission_refund_cents += apply_bps(r.refund_cents, o.commission_bps)
        m.return_logistics_cents += r.return_logistics_cents
        if r.restockable:
            m.restocked_cogs_cents += r.quantity * unit_cost[o.product_id]

    for d, spend in ad_spend.items():
        if date_from <= d <= date_to:
            slot(d).ad_spend_cents += spend
    return out
