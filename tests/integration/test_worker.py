from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.db.session import session_scope
from app.services.ingest import NewOrder, ingest_orders
from app.services.rollup import rollup_state
from app.worker import tasks
from app.worker.celery_app import celery_app
from tests.helpers import TODAY, build_dataset


def test_tasks_are_registered_and_beat_sweeps_for_dirty_sellers():
    assert {
        "app.worker.tasks.recalculate_seller",
        "app.worker.tasks.recalculate_dirty",
        "app.worker.tasks.generate_dataset",
    } <= set(celery_app.tasks)
    entry = celery_app.conf.beat_schedule["recalculate-dirty-sellers"]
    assert entry["task"] == "app.worker.tasks.recalculate_dirty" and entry["schedule"] > 0
    # A worker that dies mid-task must hand it back: safe because tasks rebuild by range.
    assert celery_app.conf.task_acks_late and celery_app.conf.task_reject_on_worker_lost


def test_recalculate_seller_reports_what_it_did(engine):
    ds = build_dataset(engine, seed=31)
    res = tasks.recalculate_seller(ds.seller_id, True)
    assert (
        res["status"] == "done" and res["days"] > 0 and res["sku_rows"] > 0 and res["seconds"] >= 0
    )
    assert tasks.recalculate_seller(987_654)["status"] == "not_found"


def test_a_second_recalculation_for_the_same_seller_is_skipped_while_one_runs(engine, r):
    ds = build_dataset(engine, seed=32)
    r.set(f"lock:recalc:{ds.seller_id}", "held-by-another-worker", ex=30)
    assert tasks.recalculate_seller(ds.seller_id, True) == {
        "seller_id": ds.seller_id,
        "status": "skipped",
        "reason": "already running",
    }
    with session_scope() as s:
        assert not rollup_state(s, ds.seller_id).ready  # nothing was computed
    assert (
        r.get(f"lock:recalc:{ds.seller_id}") == "held-by-another-worker"
    )  # not stolen or released


def test_the_sweep_picks_up_dirty_sellers_and_ignores_clean_ones(engine):
    dirty = build_dataset(engine, seed=33)
    clean = build_dataset(engine, seed=34, id_offset=20_000)
    tasks.recalculate_seller(dirty.seller_id, True)
    tasks.recalculate_seller(clean.seller_id, True)

    at = datetime.combine(TODAY - timedelta(days=3), datetime.min.time(), tzinfo=UTC)
    with session_scope() as s:
        ingest_orders(
            s,
            dirty.seller_id,
            [NewOrder(dirty.skus[min(dirty.skus)], 1, 5000, 0, 1000, 200, "delivered", at)],
        )
    out = tasks.recalculate_dirty()
    assert out == {"queued": [dirty.seller_id]}  # eager mode ran it inline
    with session_scope() as s:
        assert not rollup_state(s, dirty.seller_id).stale


def test_recalculation_bumps_the_cache_version_after_commit(engine, r):
    ds = build_dataset(engine, seed=35)
    before = r.get(f"ue:ver:{ds.seller_id}")
    tasks.recalculate_seller(ds.seller_id, True)
    assert int(r.get(f"ue:ver:{ds.seller_id}")) == int(before or 0) + 1


def test_generate_dataset_task_builds_rollups_for_every_new_seller(engine):
    res = tasks.generate_dataset(2, 400, 21, 3)
    assert res["orders"] == 400 and len(res["seller_ids"]) == 2
    with engine.connect() as c:
        ready = c.execute(
            text("SELECT count(*) FROM sellers WHERE last_recalculated_at IS NOT NULL")
        ).scalar()
        assert ready == 2
