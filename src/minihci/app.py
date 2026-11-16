import logging
import sys
import time
from typing import TextIO

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from minihci.api import hosts, metrics, scheduler, tasks, vms
from minihci.api.idempotency import IdempotencyConflict, InvalidIdempotencyKey
from minihci.config import Settings
from minihci.db import make_engine, make_session_factory
from minihci.logging import configure_logging


def create_app(
    settings: Settings,
    session_factory: sessionmaker | None = None,
    log_stream: TextIO | None = None,
) -> FastAPI:
    configure_logging(stream=log_stream or sys.stderr)
    req_log = logging.getLogger("minihci.api")
    app = FastAPI(title="MiniHCI")
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    app.state.settings = settings
    app.state.session_factory = session_factory

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        ms = round((time.perf_counter() - start) * 1000, 2)
        req_log.info(
            "request",
            extra={
                "fields": {"method": request.method, "path": request.url.path, "status": response.status_code, "ms": ms}
            },
        )
        return response

    app.include_router(hosts.router)
    app.include_router(tasks.router)
    app.include_router(vms.router)
    app.include_router(metrics.router)
    app.include_router(scheduler.router)

    @app.exception_handler(IdempotencyConflict)
    def _conflict(_: Request, exc: IdempotencyConflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "task_id": exc.task_id}, status_code=409)

    @app.exception_handler(InvalidIdempotencyKey)
    def _bad_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
