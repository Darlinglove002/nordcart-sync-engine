from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.db.models import DailySellerMetrics, Seller
from app.db.session import session_scope
from app.services import rollup as rollup_mod
from app.services.ingest import NewOrder, ingest_orders
from app.services.rollup import compute_dashboard, recalculate
from tests.helpers import TODAY, build_dataset


def _strip(payload):
    p = dict(payload)
    p.pop("meta")
    return p


def _dash(s, ds, source, **kw):
    return compute_dashboard(
        s,
        ds.seller_id,
        kw.get("d1", ds.first_day),
        kw.get("d2", ds.last_day),
        "day",
        5,
        source=source,
    )


def test_rollup_dashboard_equals_live_dashboard(engine):
    ds = build_dataset(engine, seed=11)
    with session_scope() as s:
        recalculate(s, ds.seller_id, full=True)
    with session_scope() as s:
        for kw in (
            {},
            {"d1": ds.first_day + timedelta(days=7), "d2": ds.last_day - timedelta(days=3)},
        ):
            assert _strip(_dash(s, ds, "rollup", **kw)) == _strip(_dash(s, ds, "live", **kw))


def test_recalculation_is_idempotent(engine):
    ds = build_dataset(engine, seed=12)
    with session_scope() as s:
        recalculate(s, ds.seller_id, full=True)
        a = _strip(_dash(s, ds, "rollup"))
    with session_scope() as s:
        recalculate(s, ds.seller_id, full=True)
        b = _strip(_dash(s, ds, "rollup"))
    assert a == b


def _new_order(day, sku, **kw):
    base = dict(
        sku=sku, quantity=2, unit_price_cents=10_000, discount_cents=0, commission_bps=1000,
        logistics_cents=300, status="delivered",
        ordered_at=datetime(day.year, day.month, day.day, 12, tzinfo=UTC),
    )  # fmt: skip
    return NewOrder(**{**base, **kw})


def test_incremental_recalc_touches_only_days_from_dirty_since(engine):
    ds = build_dataset(engine, seed=13)
    with session_scope() as s:
        recalculate(s, ds.seller_id, full=True)
        before = dict(
            s.execute(
                select(DailySellerMetrics.day, DailySellerMetrics.computed_at).where(
                    DailySellerMetrics.seller_id == ds.seller_id
                )
            ).all()
        )

    late = TODAY - timedelta(days=2)
    with session_scope() as s:
        ingest_orders(s, ds.seller_id, [_new_order(late, ds.skus[min(ds.skus)])])
        state = rollup_mod.rollup_state(s, ds.seller_id)
        assert state.stale  # the dashboard can say so until the worker catches up

    with session_scope() as s:
        res = recalculate(s, ds.seller_id)
        assert res.date_from == late and res.cleared_dirty
    with session_scope() as s:
        after = dict(
            s.execute(
                select(DailySellerMetrics.day, DailySellerMetrics.computed_at).where(
                    DailySellerMetrics.seller_id == ds.seller_id
                )
            ).all()
        )
        untouched = [d for d in before if d < late]
        assert untouched and all(after[d] == before[d] for d in untouched)  # not rewritten
        assert _strip(_dash(s, ds, "rollup", d2=TODAY)) == _strip(_dash(s, ds, "live", d2=TODAY))
        assert not rollup_mod.rollup_state(s, ds.seller_id).stale


def test_ingest_arriving_mid_recalculation_keeps_the_seller_dirty(engine, monkeypatch):
    """The compare-and-set on data_version: rows committed during a run must not be lost."""
    ds = build_dataset(engine, seed=14)
    with session_scope() as s:
        recalculate(s, ds.seller_id, full=True)
    with session_scope() as s:
        ingest_orders(
            s, ds.seller_id, [_new_order(TODAY - timedelta(days=5), ds.skus[min(ds.skus)])]
        )

    real = rollup_mod.fetch_sku_daily_live
    injected = {"done": False}

    def fetch_then_ingest_elsewhere(s, seller_id, d1, d2):
        out = real(s, seller_id, d1, d2)
        if not injected["done"]:  # another transaction commits while this one is computing
            injected["done"] = True
            with session_scope() as other:
                ingest_orders(
                    other, seller_id, [_new_order(TODAY - timedelta(days=1), ds.skus[min(ds.skus)])]
                )
        return out

    monkeypatch.setattr(rollup_mod, "fetch_sku_daily_live", fetch_then_ingest_elsewhere)
    with session_scope() as s:
        res = recalculate(s, ds.seller_id)
    assert not res.cleared_dirty
    with session_scope() as s:
        assert rollup_mod.rollup_state(s, ds.seller_id).stale  # still dirty

    monkeypatch.setattr(rollup_mod, "fetch_sku_daily_live", real)
    with session_scope() as s:
        assert recalculate(s, ds.seller_id).cleared_dirty
    with session_scope() as s:
        assert _strip(_dash(s, ds, "rollup", d2=TODAY)) == _strip(_dash(s, ds, "live", d2=TODAY))


def test_unknown_seller_and_empty_seller(engine):
    with session_scope() as s:
        assert recalculate(s, 99_999) is None
        sid = s.execute(
            Seller.__table__.insert()
            .values(name="Empty", api_key_hash="e" * 64)
            .returning(Seller.id)
        ).scalar_one()
    with session_scope() as s:
        res = recalculate(s, sid)
        assert res.days == 0
        assert s.execute(select(func.count()).select_from(DailySellerMetrics)).scalar() == 0
        assert s.execute(select(Seller.last_recalculated_at).where(Seller.id == sid)).scalar()
