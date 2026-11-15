from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from minihci.config import Settings
from minihci.db import make_engine, make_session_factory


def create_app(settings: Settings, session_factory: sessionmaker | None = None) -> FastAPI:
    app = FastAPI(title="MiniHCI")
    if session_factory is None:
        session_factory = make_session_factory(make_engine(settings.database_url))
    app.state.settings = settings
    app.state.session_factory = session_factory

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
