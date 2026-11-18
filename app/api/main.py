from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes import router
from app.cache import get_redis
from app.config import get_settings
from app.db.session import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("nordcart")


@asynccontextmanager
async def lifespan(_: FastAPI):
    get_settings()  # fail at startup, not on the first request, if ADMIN_API_KEY is missing/weak
    yield
    get_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="nordcart-sync-engine",
        version="0.1.0",
        description="Unit-economics dashboards for marketplace sellers.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def timing_and_headers(request: Request, call_next) -> Response:
        t0 = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Process-Time-Ms"] = f"{(time.perf_counter() - t0) * 1000:.2f}"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the details, tell the client nothing about internals.
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal error"}, status_code=500)

    @app.get("/health", tags=["ops"])
    def health() -> JSONResponse:
        checks = {}
        try:
            with get_engine().connect() as c:
                c.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception:
            checks["postgres"] = "down"
        try:
            get_redis().ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "down"  # the API still works without it, just slower
        code = 200 if checks["postgres"] == "ok" else 503
        return JSONResponse(
            {"status": "ok" if code == 200 else "degraded", **checks}, status_code=code
        )

    app.include_router(router)
    return app


app = create_app()
