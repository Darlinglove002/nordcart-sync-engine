"""Authentication and per-tenant authorisation.

Two kinds of caller: the admin key (operations, ingestion, seeding) and a per-seller key.
A seller key only ever opens that seller's data; asking for anyone else's id returns 404,
exactly like a seller that does not exist, so ids cannot be probed.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Annotated, cast

from fastapi import Depends, Header, HTTPException, status
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cache import DashboardCache, get_redis
from app.config import get_settings
from app.db.models import Seller
from app.db.session import get_session

log = logging.getLogger(__name__)

AUTH_CACHE_SECONDS = 60


@dataclass(frozen=True, slots=True)
class Principal:
    is_admin: bool
    seller_id: int | None = None


def get_cache() -> DashboardCache:
    s = get_settings()
    return DashboardCache(get_redis(), s.cache_ttl_seconds, s.cache_lock_seconds)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing or invalid API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def auth_cache_key(key_hash: str) -> str:
    return f"auth:{key_hash}"


def _seller_for_key(key_hash: str, session: Session) -> int | None:
    """api key hash -> seller id. Redis first so a cached dashboard needs no DB round trip."""
    r = get_redis()
    try:
        hit = r.get(auth_cache_key(key_hash))
        if hit is not None:
            return int(cast(str, hit))
    except RedisError:
        log.warning("redis unavailable for auth lookup; using the database")

    seller_id = session.execute(
        select(Seller.id).where(Seller.api_key_hash == key_hash)
    ).scalar_one_or_none()
    if seller_id is not None:  # negative results are never cached
        try:
            r.set(auth_cache_key(key_hash), seller_id, ex=AUTH_CACHE_SECONDS)
        except RedisError:
            pass
    return seller_id


def get_principal(
    session: Annotated[Session, Depends(get_session)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal:
    if not x_api_key or len(x_api_key) > 256:
        raise _unauthorized()
    if hmac.compare_digest(x_api_key.encode(), get_settings().admin_api_key.encode()):
        return Principal(is_admin=True)
    seller_id = _seller_for_key(hashlib.sha256(x_api_key.encode()).hexdigest(), session)
    if seller_id is None:
        raise _unauthorized()
    return Principal(is_admin=False, seller_id=seller_id)


def require_admin(principal: Annotated[Principal, Depends(get_principal)]) -> Principal:
    if not principal.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin key required")
    return principal


def authorize_seller(
    seller_id: int, principal: Annotated[Principal, Depends(get_principal)]
) -> int:
    """Path dependency: the caller may see this seller, or it 'does not exist'."""
    if principal.is_admin or principal.seller_id == seller_id:
        return seller_id
    raise HTTPException(status.HTTP_404_NOT_FOUND, "seller not found")
