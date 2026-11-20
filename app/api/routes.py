from __future__ import annotations

import logging
import secrets
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from celery.result import AsyncResult
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.deps import (
    Principal,
    auth_cache_key,
    authorize_seller,
    get_cache,
    get_principal,
    require_admin,
)
from app.api.schemas import JobAccepted, OrdersIn, SeedIn
from app.cache import DashboardCache, get_redis
from app.config import get_settings
from app.db.models import Seller
from app.db.session import get_session
from app.generator.synthetic import hash_api_key
from app.services.ingest import NewOrder, UnknownSkus, ingest_orders
from app.services.rollup import compute_dashboard, rollup_state, utc_today
from app.worker.celery_app import celery_app
from app.worker.tasks import generate_dataset, recalculate_seller

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")

SessionDep = Annotated[Session, Depends(get_session)]
CacheDep = Annotated[DashboardCache, Depends(get_cache)]
SellerDep = Annotated[int, Depends(authorize_seller)]


def _enqueue(
    task: Any, *args: Any, dedupe_key: str | None = None, dedupe_seconds: int = 30
) -> str | None:
    """Send a task, never letting a broker outage break an API request."""
    try:
        if dedupe_key and not get_redis().set(dedupe_key, "1", nx=True, ex=dedupe_seconds):
            return None
        return task.delay(*args).id
    except Exception:  # broker/redis down: the beat sweep will retry because the seller is dirty
        log.exception("could not enqueue %s", getattr(task, "name", task))
        return None


@router.get("/sellers/{seller_id}/dashboard")
def dashboard(
    seller_id: SellerDep,
    principal: Annotated[Principal, Depends(get_principal)],
    session: SessionDep,
    cache: CacheDep,
    response: Response,
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: Literal["day", "week", "month"] = "day",
    top_skus: Annotated[int, Query(ge=1, le=50)] = 5,
    source: Annotated[
        Literal["auto", "live"],
        Query(description="`live` recomputes from raw orders (admin only; for verification)."),
    ] = "auto",
    use_cache: Annotated[
        bool, Query(alias="cache", description="`false` skips Redis (admin only).")
    ] = True,
) -> dict[str, Any]:
    settings = get_settings()
    if (source == "live" or not use_cache) and not principal.is_admin:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "admin key required for source=live / cache=false"
        )

    d2 = date_to or utc_today()
    d1 = date_from or (d2 - timedelta(days=29))
    if d1 > d2:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "date_from is after date_to")
    if (d2 - d1).days + 1 > settings.max_range_days:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"range is limited to {settings.max_range_days} days",
        )

    def compute() -> dict[str, Any]:
        if source == "live":
            return compute_dashboard(session, seller_id, d1, d2, group_by, top_skus, source="live")
        state = rollup_state(session, seller_id)
        if state is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "seller not found")
        if not state.ready:
            # Never calculated: answer correctly from raw data now, build the rollup in the background.
            _enqueue(
                recalculate_seller,
                seller_id,
                True,
                dedupe_key=f"ue:enq:{seller_id}",
                dedupe_seconds=60,
            )
            return compute_dashboard(session, seller_id, d1, d2, group_by, top_skus, source="live")
        return compute_dashboard(session, seller_id, d1, d2, group_by, top_skus, source="rollup")

    if use_cache:
        payload, how = cache.get_or_compute(
            seller_id, (d1, d2, group_by, top_skus, source), compute
        )
    else:
        payload, how = compute(), "bypass"

    response.headers["X-Cache"] = how.upper()
    response.headers["Cache-Control"] = "private, no-store"
    return payload


@router.get("/sellers/{seller_id}/status")
def seller_status(seller_id: SellerDep, session: SessionDep) -> dict[str, Any]:
    row = session.execute(
        select(Seller.last_recalculated_at, Seller.dirty_since, Seller.data_version).where(
            Seller.id == seller_id
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "seller not found")
    last, dirty, version = row
    return {
        "seller_id": seller_id,
        "rollup_ready": last is not None,
        "rollup_updated_at": last.isoformat(timespec="seconds") if last else None,
        "pending_changes_since": dirty.isoformat() if dirty else None,
        "data_version": version,
    }


@router.post("/sellers/{seller_id}/recalculate", status_code=status.HTTP_202_ACCEPTED)
def recalculate(
    seller_id: SellerDep,
    principal: Annotated[Principal, Depends(get_principal)],
    full: bool = False,
) -> JobAccepted:
    if full and not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin key required for full=true")
    # One request per seller per 30 s: this endpoint must not become a way to flood the workers.
    try:
        allowed = get_redis().set(f"ue:recalc-req:{seller_id}", "1", nx=True, ex=30)
    except RedisError:
        allowed = True
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "recalculation was requested moments ago",
            headers={"Retry-After": "30"},
        )
    job_id = _enqueue(recalculate_seller, seller_id, full)
    if job_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "job queue unavailable")
    return JobAccepted(job_id=job_id)


@router.post("/sellers/{seller_id}/orders", status_code=status.HTTP_202_ACCEPTED)
def post_orders(
    seller_id: SellerDep,
    body: OrdersIn,
    _admin: Annotated[Principal, Depends(require_admin)],
    session: SessionDep,
    cache: CacheDep,
) -> dict[str, Any]:
    """Ingest a batch of orders. The dashboard is marked stale until the worker catches up."""
    try:
        earliest = ingest_orders(
            session, seller_id, [NewOrder(**o.model_dump()) for o in body.orders]
        )
    except UnknownSkus as e:
        session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e
    session.commit()
    cache.invalidate(seller_id)  # so the next read reports `stale: true` instead of old numbers
    log.info(
        "seller %s: %d orders accepted, earliest day %s",
        seller_id,
        len(body.orders),
        earliest.isoformat(),
    )
    job_id = _enqueue(
        recalculate_seller, seller_id, False, dedupe_key=f"ue:enq:{seller_id}", dedupe_seconds=5
    )
    return {"accepted": len(body.orders), "earliest_day": earliest.isoformat(), "job_id": job_id}


# --------------------------------------------------------------------------------- admin


@router.post("/admin/seed", status_code=status.HTTP_202_ACCEPTED)
def seed(body: SeedIn, _admin: Annotated[Principal, Depends(require_admin)]) -> JobAccepted:
    job_id = _enqueue(generate_dataset, body.sellers, body.orders, body.days, body.seed)
    if job_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "job queue unavailable")
    log.info(
        "seed job %s queued: %d sellers, %d orders, %d days",
        job_id,
        body.sellers,
        body.orders,
        body.days,
    )
    return JobAccepted(job_id=job_id, status_url=f"/api/v1/admin/jobs/{job_id}")


@router.get("/admin/jobs/{job_id}")
def job(job_id: str, _admin: Annotated[Principal, Depends(require_admin)]) -> dict[str, Any]:
    res = AsyncResult(job_id, app=celery_app)
    out: dict[str, Any] = {"job_id": job_id, "state": res.state}
    if res.successful():
        out["result"] = res.result
    elif res.failed():
        out["error"] = type(res.result).__name__  # no traceback or message: it may hold internals
    return out


@router.post("/admin/sellers/{seller_id}/rotate-key")
def rotate_key(
    seller_id: int,
    _admin: Annotated[Principal, Depends(require_admin)],
    session: SessionDep,
) -> dict[str, Any]:
    """Issue a new key (returned once) and kill the old one immediately, cache included."""
    old = session.execute(
        select(Seller.api_key_hash).where(Seller.id == seller_id)
    ).scalar_one_or_none()
    if old is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "seller not found")
    new_key = secrets.token_urlsafe(32)
    session.execute(
        update(Seller).where(Seller.id == seller_id).values(api_key_hash=hash_api_key(new_key))
    )
    session.commit()
    try:
        get_redis().delete(auth_cache_key(old))
    except RedisError:
        log.warning("could not drop cached auth entry for seller %s", seller_id)
    log.info("seller %s: API key rotated", seller_id)  # the key itself is never logged
    return {"seller_id": seller_id, "api_key": new_key}
