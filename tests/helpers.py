"""Small hand-rolled datasets for integration tests."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, insert

from app.db.models import AdSpend, Order, Product, Return, Seller
from app.domain.economics import OrderRow, ReturnRow
from app.generator.synthetic import hash_api_key

TODAY = date(2026, 11, 1)


@dataclass
class Dataset:
    seller_id: int
    api_key: str
    orders: list[OrderRow]
    returns: list[ReturnRow]
    unit_cost: dict[int, int]
    ad_spend: dict[date, int]
    skus: dict[int, str]  # product id -> sku
    first_day: date
    last_day: date


def _at(d: date, rng: random.Random) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(seconds=rng.randint(0, 86_399))


def build_dataset(
    engine: Engine,
    *,
    seed: int = 7,
    n_orders: int = 600,
    days: int = 40,
    name: str = "Test Seller",
    id_offset: int = 0,
) -> Dataset:
    rng = random.Random(seed)
    first = TODAY - timedelta(days=days - 1)
    key = f"key-{seed}-{rng.getrandbits(64):x}"

    with engine.begin() as c:
        sid = c.execute(
            insert(Seller).values(name=name, api_key_hash=hash_api_key(key)).returning(Seller.id)
        ).scalar_one()
        specs = []
        for i in range(12):
            price = rng.randint(500, 20_000)
            specs.append(
                {"seller_id": sid, "sku": f"T{seed}-{i:02d}", "category": rng.choice(["home", "toys"]),
                 "cost_cents": int(price * rng.uniform(0.4, 1.05)), "price_cents": price}
            )  # fmt: skip
        pids = list(c.execute(insert(Product).returning(Product.id), specs).scalars())
    unit_cost = {pid: sp["cost_cents"] for pid, sp in zip(pids, specs, strict=True)}
    skus = {pid: sp["sku"] for pid, sp in zip(pids, specs, strict=True)}

    orders: list[OrderRow] = []
    order_rows = []
    for oid in range(id_offset + 1, id_offset + n_orders + 1):
        pid = rng.choice(pids)
        d = first + timedelta(days=rng.randrange(days))
        qty = rng.choice([1, 1, 1, 2, 3])
        price = rng.randint(500, 20_000)
        disc = rng.choice([0, 0, 0, int(qty * price * 0.1)])
        bps = rng.choice([800, 1000, 1200, 1500])
        logi = rng.randint(100, 1500)
        status = "cancelled" if rng.random() < 0.1 else "delivered"
        orders.append(OrderRow(oid, pid, d, qty, price, disc, bps, logi, status))
        order_rows.append(
            {"id": oid, "seller_id": sid, "product_id": pid, "quantity": qty, "unit_price_cents": price,
             "discount_cents": disc, "commission_bps": bps, "logistics_cents": logi, "status": status,
             "ordered_at": _at(d, rng), "day": d}
        )  # fmt: skip

    # One huge basket: revenue * bps (~1e12) exceeds int32, so a missing bigint cast shows up as an error.
    big = id_offset + n_orders + 1
    orders.append(OrderRow(big, pids[0], first, 100, 19_000_000, 0, 1000, 500, "delivered"))
    order_rows.append(
        {"id": big, "seller_id": sid, "product_id": pids[0], "quantity": 100,
         "unit_price_cents": 19_000_000, "discount_cents": 0, "commission_bps": 1000,
         "logistics_cents": 500, "status": "delivered", "ordered_at": _at(first, rng), "day": first}
    )  # fmt: skip

    returns: list[ReturnRow] = []
    return_rows = []
    for o in orders:
        if o.status != "delivered" or rng.random() > 0.15:
            continue
        rd = o.day + timedelta(days=rng.randint(0, 10))
        rq = rng.randint(1, o.quantity)
        refund = (o.quantity * o.unit_price_cents - o.discount_cents) * rq // o.quantity
        rl = rng.randint(0, 800)
        restock = rng.random() < 0.7
        returns.append(ReturnRow(o.id, rd, rq, refund, rl, restock))
        return_rows.append(
            {"order_id": o.id, "seller_id": sid, "quantity": rq, "refund_cents": refund,
             "return_logistics_cents": rl, "restockable": restock, "returned_at": _at(rd, rng), "day": rd}
        )  # fmt: skip

    ad: dict[date, int] = {}
    ad_rows = []
    for i in range(days):
        d = first + timedelta(days=i)
        if rng.random() < 0.8:
            ad[d] = rng.randint(100, 5000)
            ad_rows.append({"seller_id": sid, "day": d, "spend_cents": ad[d]})

    with engine.begin() as c:
        c.execute(insert(Order), order_rows)
        c.execute(insert(Return), return_rows)
        c.execute(insert(AdSpend), ad_rows)
        c.exec_driver_sql("SELECT setval(pg_get_serial_sequence('orders','id'), %s)", (big,))
    last = max(max(o.day for o in orders), max((r.day for r in returns), default=first))
    return Dataset(sid, key, orders, returns, unit_cost, ad, skus, first, last)
