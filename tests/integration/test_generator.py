from __future__ import annotations

import pytest
from sqlalchemy import text

from app.generator.synthetic import generate, hash_api_key, reset_all
from tests.helpers import TODAY


def fingerprint(engine):
    """Everything about the data except surrogate ids, names and random API keys."""
    with engine.connect() as c:
        return {
            "orders": c.execute(
                text(
                    "SELECT count(*), sum(quantity), sum(unit_price_cents), sum(discount_cents),"
                    " sum(logistics_cents), count(*) FILTER (WHERE status = 'cancelled'),"
                    " min(day), max(day) FROM orders"
                )
            ).one(),
            "returns": c.execute(
                text("SELECT count(*), sum(refund_cents), sum(return_logistics_cents) FROM returns")
            ).one(),
            "ads": c.execute(text("SELECT count(*), sum(spend_cents) FROM ad_spend")).one(),
            "products": c.execute(
                text("SELECT count(*), sum(cost_cents), sum(price_cents) FROM products")
            ).one(),
            "by_day": c.execute(
                text("SELECT day, count(*) FROM orders GROUP BY day ORDER BY day")
            ).all(),
        }


def test_same_seed_and_date_give_identical_data(engine):
    generate(engine, sellers=2, orders=3000, days=60, seed=5, today=TODAY)
    a = fingerprint(engine)
    reset_all(engine)
    generate(engine, sellers=2, orders=3000, days=60, seed=5, today=TODAY)
    assert fingerprint(engine) == a


def test_different_seed_gives_different_data(engine):
    generate(engine, sellers=2, orders=3000, days=60, seed=5, today=TODAY)
    a = fingerprint(engine)
    reset_all(engine)
    generate(engine, sellers=2, orders=3000, days=60, seed=6, today=TODAY)
    assert fingerprint(engine) != a


def test_counts_ranges_and_realism(engine):
    res = generate(engine, sellers=3, orders=5000, days=90, seed=1, today=TODAY)
    assert (res.orders, len(res.sellers), res.products) == (5000, 3, 600)
    with engine.connect() as c:
        lo, hi = c.execute(text("SELECT min(day), max(day) FROM orders")).one()
        assert hi <= TODAY and (TODAY - lo).days <= 89
        # no return arrives from the future
        assert (
            c.execute(text("SELECT count(*) FROM returns WHERE day > :t"), {"t": TODAY}).scalar()
            == 0
        )
        # a return always belongs to a delivered order of the same seller
        bad = c.execute(
            text(
                "SELECT count(*) FROM returns r JOIN orders o ON o.id = r.order_id"
                " WHERE o.status <> 'delivered' OR o.seller_id <> r.seller_id"
            )
        ).scalar()
        assert bad == 0
        assert (
            c.execute(text("SELECT count(*) FROM orders WHERE status = 'cancelled'")).scalar() > 0
        )
        # the dashboard has something to find: SKUs that cannot cover their own cost
        assert (
            c.execute(
                text("SELECT count(*) FROM products WHERE cost_cents >= price_cents * 0.8")
            ).scalar()
            > 0
        )


def test_api_keys_are_only_stored_hashed(engine):
    res = generate(engine, sellers=2, orders=200, days=14, seed=2, today=TODAY)
    with engine.connect() as c:
        stored = {r[0] for r in c.execute(text("SELECT api_key_hash FROM sellers"))}
    assert stored == {hash_api_key(s.api_key) for s in res.sellers}
    assert not any(s.api_key in stored for s in res.sellers)
    assert len({s.api_key for s in res.sellers}) == 2


def test_appending_does_not_collide_with_existing_rows(engine):
    generate(engine, sellers=1, orders=500, days=30, seed=1, today=TODAY)
    generate(engine, sellers=1, orders=500, days=30, seed=2, today=TODAY)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM orders")).scalar() == 1000
        # the identity sequence moved past the explicit ids, so a normal insert still works
        c.execute(text("SELECT nextval(pg_get_serial_sequence('orders', 'id'))"))


@pytest.mark.parametrize("kwargs", [{"sellers": 0}, {"orders": 0}, {"days": 3}])
def test_rejects_nonsense_sizes(engine, kwargs):
    with pytest.raises(ValueError):
        generate(engine, today=TODAY, **kwargs)
