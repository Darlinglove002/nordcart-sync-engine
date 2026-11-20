"""Order ingestion: append rows, mark the seller dirty, let the worker catch up."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.db.models import Order, Product, Seller


@dataclass(slots=True)
class NewOrder:
    sku: str
    quantity: int
    unit_price_cents: int
    discount_cents: int
    commission_bps: int
    logistics_cents: int
    status: str
    ordered_at: datetime


class UnknownSkus(Exception):
    def __init__(self, skus: set[str]):
        super().__init__(f"unknown SKUs: {sorted(skus)[:5]}")
        self.skus = skus


def ingest_orders(s: Session, seller_id: int, orders: list[NewOrder]) -> date:
    """Insert orders and mark the seller dirty. Returns the earliest affected day.

    The caller commits. `data_version` is bumped in the same transaction as the rows, which
    is what makes the recalculation's compare-and-set safe.
    """
    skus = {o.sku for o in orders}
    found: dict[str, int] = dict(
        s.execute(
            select(Product.sku, Product.id).where(
                Product.seller_id == seller_id, Product.sku.in_(skus)
            )
        )
        .tuples()
        .all()
    )
    missing = skus - found.keys()
    if missing:
        raise UnknownSkus(missing)

    rows: list[dict[str, Any]] = []
    for o in orders:
        at = o.ordered_at.astimezone(UTC)
        rows.append(
            {
                "seller_id": seller_id,
                "product_id": found[o.sku],
                "quantity": o.quantity,
                "unit_price_cents": o.unit_price_cents,
                "discount_cents": o.discount_cents,
                "commission_bps": o.commission_bps,
                "logistics_cents": o.logistics_cents,
                "status": o.status,
                "ordered_at": at,
                "day": at.date(),
            }
        )
    s.execute(insert(Order), rows)

    earliest = min(r["day"] for r in rows)
    s.execute(
        update(Seller)
        .where(Seller.id == seller_id)
        .values(
            data_version=Seller.data_version + 1,
            dirty_since=func.least(func.coalesce(Seller.dirty_since, earliest), earliest),
        )
    )
    return earliest
