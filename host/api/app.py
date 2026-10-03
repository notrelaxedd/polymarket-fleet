"""FastAPI application factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from host import db
from host.api import dl, jobs, owner, workers
from host.bundle import build_bundle
from host.config import Config
from host.errors import QueueError

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


class BodySizeLimit:
    """Reject request bodies above MAX_BODY_BYTES with 413 (worker writes are small)."""

    def __init__(self, app: Any, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            for name, value in scope.get("headers", []):
                if name == b"content-length" and value.isdigit() and int(value) > self.limit:
                    response = JSONResponse({"detail": "request body too large"}, status_code=413)
                    await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def _validation_detail(exc: RequestValidationError) -> str:
    """A one-line description of the first validation problem."""
    errors = exc.errors()
    if not errors:
        return "invalid request"
    first = errors[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    return f"{where}: {first.get('msg', 'invalid')}"


def create_app(config: Config) -> FastAPI:
    """Build the app; the pool opens in the lifespan so tests can control it."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool = db.make_pool(config.database_url)
        try:
            yield
        finally:
            app.state.pool.close()

    app = FastAPI(title="polymarket-fleet host", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.bundle = build_bundle()
    log.info("worker bundle code_version=%s", app.state.bundle.code_version)

    @app.exception_handler(QueueError)
    async def queue_error(_: Request, exc: QueueError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse({"detail": _validation_detail(exc)}, status_code=400)

    app.include_router(workers.router)
    app.include_router(jobs.router)
    app.include_router(owner.router)
    app.include_router(owner.health_router)
    app.include_router(dl.router)
    app.add_middleware(BodySizeLimit)
    return app
