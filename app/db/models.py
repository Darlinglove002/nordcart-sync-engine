from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Seller(Base):
    __tablename__ = "sellers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    # SHA-256 of a 256-bit random key. A slow password hash would add nothing for keys that
    # are not human-chosen, and this lookup is on the hot path of every request.
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Incremented on every ingest. A recalculation only clears `dirty_since` if the version it
    # started from is still current, so data that arrives mid-run is never lost.
    data_version: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    # Earliest day touched by data not yet reflected in the rollups. NULL = rollups are current.
    dirty_since: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_recalculated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("seller_id", "sku", name="uq_products_seller_sku"),
        CheckConstraint("cost_cents >= 0 AND price_cents > 0", name="ck_products_money"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"))
    sku: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32))
    cost_cents: Mapped[int] = mapped_column(Integer)
    price_cents: Mapped[int] = mapped_column(Integer)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_seller_day", "seller_id", "day"),
        CheckConstraint("status IN ('delivered', 'cancelled')", name="ck_orders_status"),
        CheckConstraint("quantity > 0", name="ck_orders_quantity"),
        # Keeps every per-order money column inside int4 (refunds, discounts, ...). The
        # aggregation still casts to bigint because revenue * bps is ~1e12 at this limit.
        CheckConstraint(
            "quantity::bigint * unit_price_cents <= 2000000000", name="ck_orders_line_total"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(SmallInteger)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    commission_bps: Mapped[int] = mapped_column(SmallInteger)
    logistics_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # UTC calendar day of `ordered_at`, stored so range scans and GROUP BY need no conversion.
    day: Mapped[date] = mapped_column(Date)


class Return(Base):
    __tablename__ = "returns"
    __table_args__ = (Index("ix_returns_seller_day", "seller_id", "day"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    seller_id: Mapped[int] = mapped_column(ForeignKey("sellers.id", ondelete="CASCADE"))
    quantity: Mapped[int] = mapped_column(SmallInteger)
    refund_cents: Mapped[int] = mapped_column(Integer)
    return_logistics_cents: Mapped[int] = mapped_column(Integer)
    restockable: Mapped[bool] = mapped_column(Boolean)
    returned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    day: Mapped[date] = mapped_column(Date)


class AdSpend(Base):
    __tablename__ = "ad_spend"

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    spend_cents: Mapped[int] = mapped_column(Integer)


class DailySellerMetrics(Base):
    """Rollup: one row per seller per day. Rebuilt by the Celery worker, read by the API."""

    __tablename__ = "daily_seller_metrics"

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    orders: Mapped[int] = mapped_column(Integer)
    units: Mapped[int] = mapped_column(Integer)
    revenue_cents: Mapped[int] = mapped_column(BigInteger)
    cogs_cents: Mapped[int] = mapped_column(BigInteger)
    commission_cents: Mapped[int] = mapped_column(BigInteger)
    logistics_cents: Mapped[int] = mapped_column(BigInteger)
    ad_spend_cents: Mapped[int] = mapped_column(BigInteger)
    returns_count: Mapped[int] = mapped_column(Integer)
    refunds_cents: Mapped[int] = mapped_column(BigInteger)
    commission_refund_cents: Mapped[int] = mapped_column(BigInteger)
    return_logistics_cents: Mapped[int] = mapped_column(BigInteger)
    restocked_cogs_cents: Mapped[int] = mapped_column(BigInteger)
    net_profit_cents: Mapped[int] = mapped_column(BigInteger)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class DailySkuMetrics(Base):
    """Rollup: one row per seller, day and product. Powers the top / worst SKU lists."""

    __tablename__ = "daily_sku_metrics"

    seller_id: Mapped[int] = mapped_column(
        ForeignKey("sellers.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    orders: Mapped[int] = mapped_column(Integer)
    units: Mapped[int] = mapped_column(Integer)
    revenue_cents: Mapped[int] = mapped_column(BigInteger)
    cogs_cents: Mapped[int] = mapped_column(BigInteger)
    commission_cents: Mapped[int] = mapped_column(BigInteger)
    logistics_cents: Mapped[int] = mapped_column(BigInteger)
    returns_count: Mapped[int] = mapped_column(Integer)
    refunds_cents: Mapped[int] = mapped_column(BigInteger)
    commission_refund_cents: Mapped[int] = mapped_column(BigInteger)
    return_logistics_cents: Mapped[int] = mapped_column(BigInteger)
    restocked_cogs_cents: Mapped[int] = mapped_column(BigInteger)
    net_profit_cents: Mapped[int] = mapped_column(BigInteger)
