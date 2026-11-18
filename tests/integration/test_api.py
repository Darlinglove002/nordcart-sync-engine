from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore", message=".*httpx.*starlette.testclient.*")

from fastapi.testclient import TestClient  # noqa: E402

from app.api import routes  # noqa: E402
from app.api.deps import get_cache  # noqa: E402
from app.api.main import app  # noqa: E402
from app.cache import DashboardCache  # noqa: E402
from app.worker.tasks import recalculate_seller  # noqa: E402
from tests.helpers import Dataset, build_dataset  # noqa: E402

ADMIN = {"X-API-Key": "test-admin-key-0123456789abcdef-xyz"}


@pytest.fixture()
def client(services):
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def ds(engine) -> Dataset:
    d = build_dataset(engine, seed=21)
    recalculate_seller(d.seller_id, True)  # eager: runs inline, like the worker would
    return d


def url(ds, suffix="dashboard"):
    return f"/api/v1/sellers/{ds.seller_id}/{suffix}"


def params(ds, **kw):
    return {"date_from": ds.first_day.isoformat(), "date_to": ds.last_day.isoformat(), **kw}


def seller_headers(ds):
    return {"X-API-Key": ds.api_key}


# ------------------------------------------------------------------------ authentication


def test_health_needs_no_key_and_reports_dependencies(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


@pytest.mark.parametrize(
    "headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "nope"}, {"X-API-Key": "x" * 300}]
)
def test_missing_or_wrong_key_is_401(client, ds, headers):
    r = client.get(url(ds), params=params(ds), headers=headers)
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "ApiKey"


def test_seller_key_opens_own_data_and_admin_opens_everything(client, ds):
    assert client.get(url(ds), params=params(ds), headers=seller_headers(ds)).status_code == 200
    assert client.get(url(ds), params=params(ds), headers=ADMIN).status_code == 200


def test_a_seller_cannot_see_another_seller_and_cannot_tell_it_exists(client, ds, engine):
    other = build_dataset(engine, seed=22, name="Other", id_offset=50_000)
    mine = seller_headers(ds)
    foreign = client.get(
        f"/api/v1/sellers/{other.seller_id}/dashboard", params=params(other), headers=mine
    )
    missing = client.get("/api/v1/sellers/987654/dashboard", params=params(ds), headers=mine)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_admin_only_switches_are_forbidden_for_sellers(client, ds):
    h = seller_headers(ds)
    assert client.get(url(ds), params=params(ds, source="live"), headers=h).status_code == 403
    assert client.get(url(ds), params=params(ds, cache="false"), headers=h).status_code == 403
    assert (
        client.post(url(ds, "recalculate"), params={"full": "true"}, headers=h).status_code == 403
    )
    assert client.post("/api/v1/admin/seed", json={}, headers=h).status_code == 403
    assert client.post(url(ds, "orders"), json={"orders": []}, headers=h).status_code == 403


# --------------------------------------------------------------------------- dashboard


def test_dashboard_shape_and_values(client, ds):
    d = client.get(url(ds), params=params(ds), headers=seller_headers(ds)).json()
    assert d["seller_id"] == ds.seller_id and d["group_by"] == "day"
    t = d["totals"]
    assert t["net_profit_cents"] == (
        t["revenue_cents"] - t["cogs_cents"] - t["commission_cents"] - t["logistics_cents"]
        - t["ad_spend_cents"] - t["refunds_cents"] + t["commission_refund_cents"]
        - t["return_logistics_cents"] + t["restocked_cogs_cents"]
    )  # fmt: skip
    assert len(d["top_skus"]) == 5 and d["series"][0]["period"] <= d["series"][-1]["period"]
    assert d["meta"]["source"] == "rollup" and d["meta"]["stale"] is False


def test_rollup_answer_equals_live_answer(client, ds):
    fast = client.get(url(ds), params=params(ds, group_by="week"), headers=ADMIN).json()
    live = client.get(
        url(ds), params=params(ds, group_by="week", source="live", cache="false"), headers=ADMIN
    ).json()
    for body in (fast, live):
        body.pop("meta")
    assert fast == live


def test_second_identical_request_is_a_cache_hit_with_the_same_body(client, ds):
    h = seller_headers(ds)
    first = client.get(url(ds), params=params(ds), headers=h)
    second = client.get(url(ds), params=params(ds), headers=h)
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert first.json() == second.json()
    assert first.headers["cache-control"] == "private, no-store"
    assert "x-process-time-ms" in second.headers


def test_cache_bypass_for_admin(client, ds):
    client.get(url(ds), params=params(ds), headers=ADMIN)
    r = client.get(url(ds), params=params(ds, cache="false"), headers=ADMIN)
    assert r.headers["x-cache"] == "BYPASS"


@pytest.mark.parametrize(
    "bad",
    [
        {"date_from": "2026-02-01", "date_to": "2026-01-01"},
        {"date_from": "2020-01-01", "date_to": "2026-01-01"},  # beyond the range limit
        {"date_from": "not-a-date"},
        {"top_skus": 0},
        {"top_skus": 51},
        {"group_by": "year"},
        {"source": "magic"},
    ],
)
def test_bad_query_parameters_are_422(client, ds, bad):
    assert client.get(url(ds), params=bad, headers=ADMIN).status_code == 422


# ------------------------------------------------------------- ingestion and recalculation


def order_payload(ds, **kw):
    base = {
        "sku": ds.skus[min(ds.skus)], "quantity": 2, "unit_price_cents": 10_000, "discount_cents": 0,
        "commission_bps": 1000, "logistics_cents": 300, "status": "delivered",
        "ordered_at": f"{ds.last_day.isoformat()}T12:00:00+00:00",
    }  # fmt: skip
    return {**base, **kw}


def test_ingest_marks_stale_then_worker_catches_up_and_cache_is_invalidated(
    client, ds, monkeypatch
):
    h = seller_headers(ds)
    before = client.get(url(ds), params=params(ds), headers=h).json()
    assert client.get(url(ds), params=params(ds), headers=h).headers["x-cache"] == "HIT"

    monkeypatch.setattr(routes, "_enqueue", lambda *a, **k: None)  # worker "not running yet"
    r = client.post(url(ds, "orders"), json={"orders": [order_payload(ds)]}, headers=ADMIN)
    assert r.status_code == 202 and r.json()["accepted"] == 1

    stale = client.get(url(ds), params=params(ds), headers=h)
    assert stale.headers["x-cache"] == "MISS"  # ingest dropped the old entry
    assert stale.json()["meta"]["stale"] is True
    assert (
        stale.json()["totals"]["revenue_cents"] == before["totals"]["revenue_cents"]
    )  # not absorbed yet
    st = client.get(url(ds, "status"), headers=h).json()
    assert st["pending_changes_since"] == ds.last_day.isoformat()

    recalculate_seller(ds.seller_id)  # the worker runs
    fresh = client.get(url(ds), params=params(ds), headers=h)
    assert fresh.headers["x-cache"] == "MISS"  # recalculation invalidated again
    assert fresh.json()["meta"]["stale"] is False
    assert fresh.json()["totals"]["revenue_cents"] == before["totals"]["revenue_cents"] + 20_000
    assert client.get(url(ds, "status"), headers=h).json()["pending_changes_since"] is None


@pytest.mark.parametrize(
    "bad",
    [
        {"sku": "NO-SUCH-SKU"},
        {"ordered_at": "2026-10-01T12:00:00"},  # naive timestamp
        {"quantity": 0},
        {"quantity": 101},
        {"discount_cents": 999_999},  # more than the line total
        {"quantity": 100, "unit_price_cents": 30_000_000},  # line total over the limit
        {"status": "lost"},
        {"surprise": 1},
    ],
)
def test_ingest_rejects_bad_orders_and_stores_nothing(client, ds, bad):
    r = client.post(
        url(ds, "orders"),
        json={"orders": [order_payload(ds), order_payload(ds, **bad)]},
        headers=ADMIN,
    )
    assert r.status_code == 422
    after = client.get(url(ds, "status"), headers=ADMIN).json()
    assert after["pending_changes_since"] is None and after["data_version"] == 0  # all-or-nothing


def test_ingest_batch_limits(client, ds):
    assert client.post(url(ds, "orders"), json={"orders": []}, headers=ADMIN).status_code == 422
    too_many = {"orders": [order_payload(ds)] * 1001}
    assert client.post(url(ds, "orders"), json=too_many, headers=ADMIN).status_code == 422


def test_recalculate_endpoint_is_throttled(client, ds):
    h = seller_headers(ds)
    first = client.post(url(ds, "recalculate"), headers=h)
    second = client.post(url(ds, "recalculate"), headers=h)
    assert first.status_code == 202 and first.json()["job_id"]
    assert second.status_code == 429 and second.headers["retry-after"] == "30"


def test_never_calculated_seller_is_answered_live_and_rollup_is_built_in_background(client, engine):
    ds = build_dataset(engine, seed=23, id_offset=90_000)  # note: no recalculation yet
    h = seller_headers(ds)
    first = client.get(url(ds), params=params(ds), headers=h).json()
    assert first["meta"]["source"] == "live"  # correct answer immediately, from raw orders
    # (eager Celery ran the rebuild inline) -> the next answer comes from the rollup, same numbers
    second = client.get(url(ds), params=params(ds), headers=h).json()
    assert second["meta"]["source"] == "rollup"
    assert second["totals"] == first["totals"]


# ----------------------------------------------------------------------------- operations


def test_rotating_a_key_kills_the_old_one_immediately_even_when_cached(client, ds):
    old = seller_headers(ds)
    assert (
        client.get(url(ds), params=params(ds), headers=old).status_code == 200
    )  # warms auth cache
    new_key = client.post(f"/api/v1/admin/sellers/{ds.seller_id}/rotate-key", headers=ADMIN).json()[
        "api_key"
    ]
    assert client.get(url(ds), params=params(ds), headers=old).status_code == 401
    assert client.get(url(ds), params=params(ds), headers={"X-API-Key": new_key}).status_code == 200
    assert client.post("/api/v1/admin/sellers/424242/rotate-key", headers=ADMIN).status_code == 404


def test_seed_endpoint_queues_a_job_and_job_status_is_readable(client, engine):
    r = client.post(
        "/api/v1/admin/seed", json={"sellers": 1, "orders": 300, "days": 14}, headers=ADMIN
    )
    assert r.status_code == 202
    job = client.get(r.json()["status_url"], headers=ADMIN).json()
    assert job["state"] == "SUCCESS" and job["result"]["orders"] == 300  # eager in tests
    assert "api_key" not in str(job)  # secrets never travel through the result backend


@pytest.mark.parametrize(
    "body", [{"orders": 5}, {"sellers": 0}, {"days": 1}, {"orders": 99_999_999}, {"x": 1}]
)
def test_seed_validation(client, body):
    assert client.post("/api/v1/admin/seed", json=body, headers=ADMIN).status_code == 422


def test_redis_outage_degrades_to_database_not_to_errors(client, ds):
    import redis

    dead = DashboardCache(redis.Redis(port=1, socket_connect_timeout=0.2), ttl=60, lock_seconds=10)
    app.dependency_overrides[get_cache] = lambda: dead
    try:
        r = client.get(url(ds), params=params(ds), headers=ADMIN)
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 200 and r.headers["x-cache"] == "BYPASS"
    assert r.json()["totals"]["orders"] > 0


def test_unhandled_errors_do_not_leak_internals(services, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret connection string postgres://user:pw@host")

    monkeypatch.setattr(routes, "rollup_state", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        r = c.get("/api/v1/sellers/1/dashboard", headers=ADMIN)
    assert r.status_code == 500 and r.json() == {"detail": "internal error"}
