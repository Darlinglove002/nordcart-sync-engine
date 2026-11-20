from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.config import get_settings
from app.db.session import get_engine
from app.health import run_checks
from app.logging_config import configure_logging

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    get_settings()  # fail at startup, not on the first request, if ADMIN_API_KEY is missing/weak
    log.info("started")
    yield
    get_engine().dispose()
    log.info("stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="nordcart-sync-engine",
        version="0.1.0",
        description="Unit-economics dashboards for marketplace sellers.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def timing_and_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
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

    @app.get(
        "/health",
        tags=["ops"],
        responses={503: {"description": "a dependency is down; `failed` names it"}},
    )
    def health() -> JSONResponse:
        """Readiness: PostgreSQL `SELECT 1` and Redis `PING`, each with hard timeouts.

        200 when both answer, 503 with the failing components named otherwise. Request handling
        itself still fails open on a Redis outage (see app/cache.py); this endpoint reports the
        outage so an operator or orchestrator can see it.
        """
        checks = run_checks()
        failed = [name for name, ok in checks.items() if not ok]
        body: dict[str, Any] = {"status": "degraded" if failed else "ok"}
        body.update({name: "ok" if ok else "down" for name, ok in checks.items()})
        if failed:
            body["failed"] = failed
        return JSONResponse(body, status_code=503 if failed else 200)

    app.include_router(router)
    return app


app = create_app()
