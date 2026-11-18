"""Synthetic marketplace data: sellers, catalogues, orders, returns, ad spend.

Deterministic for a given (seed, today): the same arguments always produce the same rows,
which is what lets tests assert exact numbers and lets benchmarks be repeated.

Shape of the data is deliberately realistic rather than uniform:
  * a few SKUs sell a lot, most sell a little (Zipf-like popularity)
  * volume grows over the period and weekends sell more
  * categories differ in commission, logistics cost and return rate (apparel returns most)
  * ~8% of SKUs are priced so that they lose money after fees: the dashboard should find them
  * some orders are cancelled; returns arrive days after delivery
"""

from __future__ import annotations

import hashlib
import itertools
import random
import secrets
from bisect import bisect_left
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import Engine, insert, text

from app.db.models import AdSpend, Product, Seller

# category: (commission_bps, price range in cents, typical gross margin range, base logistics
#            cents range, return probability)
CATEGORIES: dict[str, tuple[int, tuple[int, int], tuple[float, float], tuple[int, int], float]] = {
    "electronics": (800, (2_500, 90_000), (0.20, 0.40), (350, 900), 0.06),
    "apparel": (1500, (1_200, 12_000), (0.45, 0.65), (250, 500), 0.18),
    "home": (1200, (1_500, 40_000), (0.33, 0.55), (600, 1_500), 0.05),
    "beauty": (1400, (800, 6_000), (0.48, 0.68), (200, 400), 0.04),
    "toys": (1000, (900, 9_000), (0.36, 0.58), (300, 800), 0.07),
}
SELLER_NAMES = [
    "Fjord & Co",
    "Nordic Nest Goods",
    "Aurora Trading",
    "Skagen Supply",
    "Lofoten Market",
    "Baltic Basket",
    "Vasa Retail",
    "Oslo Outlet",
]
LOSS_MAKING_SHARE = 0.08


@dataclass(slots=True)
class GeneratedSeller:
    id: int
    name: str
    api_key: str  # shown once; only its hash is stored


@dataclass(slots=True)
class GenerationResult:
    sellers: list[GeneratedSeller]
    products: int
    orders: int
    returns: int
    ad_days: int


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _cum(weights: list[float]) -> list[float]:
    return list(itertools.accumulate(weights))


def _pick(rng: random.Random, cum: list[float]) -> int:
    return min(bisect_left(cum, rng.random() * cum[-1]), len(cum) - 1)


def generate(
    engine: Engine,
    *,
    sellers: int = 5,
    orders: int = 50_000,
    days: int = 180,
    seed: int = 42,
    products_per_seller: int = 200,
    today: date | None = None,
) -> GenerationResult:
    if not (1 <= sellers <= 1000 and 1 <= orders <= 20_000_000 and 7 <= days <= 3650):
        raise ValueError("sellers 1..1000, orders 1..20_000_000, days 7..3650")
    rng = random.Random(seed)
    today = today or datetime.now(UTC).date()
    first_day = today - timedelta(days=days - 1)
    suffix = secrets.token_hex(2)

    # ---- sellers and catalogues (small: normal ORM-style inserts) ----------------------------
    created: list[GeneratedSeller] = []
    catalogue: dict[int, list[tuple[int, str, int]]] = {}  # seller -> (product id, category, price)
    with engine.begin() as conn:
        for i in range(sellers):
            name = f"{SELLER_NAMES[i % len(SELLER_NAMES)]} #{suffix}-{i + 1}"
            key = secrets.token_urlsafe(32)
            sid = conn.execute(
                insert(Seller)
                .values(name=name, api_key_hash=hash_api_key(key))
                .returning(Seller.id)
            ).scalar_one()
            created.append(GeneratedSeller(sid, name, key))

            specs = []
            for n in range(products_per_seller):
                cat = rng.choice(list(CATEGORIES))
                commission, (plo, phi), (mlo, mhi), _, _ = CATEGORIES[cat]
                price = rng.randint(plo, phi)
                if rng.random() < LOSS_MAKING_SHARE:
                    cost = int(price * rng.uniform(0.80, 1.00))  # cannot cover the fees
                else:
                    cost = int(price * (1 - rng.uniform(mlo, mhi)))
                specs.append(
                    {"seller_id": sid, "sku": f"SKU-{i + 1}-{n + 1:04d}", "category": cat,
                     "cost_cents": cost, "price_cents": price}
                )  # fmt: skip
            ids = conn.execute(insert(Product).returning(Product.id), specs).scalars().all()
            catalogue[sid] = [
                (pid, sp["category"], sp["price_cents"]) for pid, sp in zip(ids, specs, strict=True)
            ]

    # ---- distributions ------------------------------------------------------------------
    seller_cum = _cum([1 / (k + 1) ** 0.6 for k in range(sellers)])
    sku_cum = {
        sid: _cum([1 / (k + 1) ** 0.8 for k in range(len(items))])
        for sid, items in catalogue.items()
    }
    day_weights = []
    for d in range(days):
        day = first_day + timedelta(days=d)
        growth = 1 + 0.8 * d / days
        weekend = 1.25 if day.weekday() >= 5 else 1.0
        day_weights.append(growth * weekend)
    day_cum = _cum(day_weights)

    # ---- orders and returns (streamed through COPY) ------------------------------------
    raw = engine.raw_connection()
    n_orders = n_returns = 0
    revenue_by_day: dict[tuple[int, date], int] = {}
    returns_buf: list[tuple] = []
    try:
        with raw.cursor() as cur:
            cur.execute("SELECT coalesce(max(id), 0) FROM orders")
            order_id = cur.fetchone()[0]
            cur.execute("SELECT coalesce(max(id), 0) FROM returns")
            return_id = cur.fetchone()[0]

            with cur.copy(
                "COPY orders (id, seller_id, product_id, quantity, unit_price_cents, discount_cents,"
                " commission_bps, logistics_cents, status, ordered_at, day) FROM STDIN"
            ) as cp:
                for _i in range(orders):
                    sid = created[_pick(rng, seller_cum)].id
                    pid, cat, price = catalogue[sid][_pick(rng, sku_cum[sid])]
                    commission, _, _, (llo, lhi), ret_p = CATEGORIES[cat]
                    qty = rng.choices((1, 2, 3, 4, 5), (80, 14, 4, 1.5, 0.5))[0]
                    # Prices move a little around the list price (promotions, repricing).
                    unit_price = max(1, int(price * rng.uniform(0.95, 1.05)))
                    line = qty * unit_price
                    discount = int(line * rng.uniform(0.05, 0.20)) if rng.random() < 0.30 else 0
                    logistics = rng.randint(llo, lhi) + (qty - 1) * (llo // 3)
                    day_idx = _pick(rng, day_cum)
                    day = first_day + timedelta(days=day_idx)
                    at = datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(
                        seconds=rng.randint(0, 86_399)
                    )
                    status = "cancelled" if rng.random() < 0.05 else "delivered"
                    order_id += 1
                    n_orders += 1
                    cp.write_row(
                        (order_id, sid, pid, qty, unit_price, discount, commission, logistics,
                         status, at, day)
                    )  # fmt: skip
                    if status != "delivered":
                        continue
                    revenue = line - discount
                    revenue_by_day[(sid, day)] = revenue_by_day.get((sid, day), 0) + revenue
                    if rng.random() < ret_p:
                        rday = day + timedelta(days=rng.randint(3, 25))
                        if rday > today:  # has not come back yet
                            continue
                        if qty > 1 and rng.random() < 0.5:
                            rqty, refund = 1, revenue // qty
                        else:
                            rqty, refund = qty, revenue
                        rat = datetime(rday.year, rday.month, rday.day, tzinfo=UTC) + timedelta(
                            seconds=rng.randint(0, 86_399)
                        )
                        returns_buf.append(
                            (sid, order_id, rqty, refund, int(logistics * rng.uniform(0.6, 1.2)),
                             rng.random() < 0.70, rat, rday)
                        )  # fmt: skip

            with cur.copy(
                "COPY returns (id, order_id, seller_id, quantity, refund_cents,"
                " return_logistics_cents, restockable, returned_at, day) FROM STDIN"
            ) as cp:
                for sid, oid, rqty, refund, rlog, restock, rat, rday in returns_buf:
                    return_id += 1
                    n_returns += 1
                    cp.write_row((return_id, oid, sid, rqty, refund, rlog, restock, rat, rday))

            # Identity columns were filled explicitly; move the sequences past the new ids.
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('orders', 'id'), %s)", (max(order_id, 1),)
            )
            cur.execute(
                "SELECT setval(pg_get_serial_sequence('returns', 'id'), %s)", (max(return_id, 1),)
            )
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    # ---- ad spend: a few percent of the day's revenue, noisy ------------------------------
    ad_rows = [
        {"seller_id": sid, "day": day, "spend_cents": int(rev * rng.uniform(0.03, 0.09))}
        for (sid, day), rev in sorted(revenue_by_day.items())
    ]
    if ad_rows:
        with engine.begin() as conn:
            conn.execute(insert(AdSpend), ad_rows)

    with engine.begin() as conn:
        conn.execute(text("ANALYZE orders"))
        conn.execute(text("ANALYZE returns"))
    return GenerationResult(
        created, sum(map(len, catalogue.values())), n_orders, n_returns, len(ad_rows)
    )


def reset_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE daily_sku_metrics, daily_seller_metrics, ad_spend, returns, orders,"
                " products, sellers RESTART IDENTITY CASCADE"
            )
        )
