from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from minihci.api import hosts, tasks, vms
from minihci.api.idempotency import IdempotencyConflict, InvalidIdempotencyKey
from minihci.config import Settings
from minihci.db import make_engine, make_session_factory


def create_app(settings: Settings, session_factory: sessionmaker | None = None) -> FastAPI:
    app = FastAPI(title="MiniHCI")
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    app.state.settings = settings
    app.state.session_factory = session_factory

    app.include_router(hosts.router)
    app.include_router(tasks.router)
    app.include_router(vms.router)

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
