import os
from pathlib import Path

from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]
TEST_DB_URL = os.environ.get(
    "MINIHCI_TEST_DATABASE_URL",
    "postgresql+psycopg://minihci:minihci@localhost:5433/minihci_test",
)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def truncate_if_present(engine) -> None:
    """Clear leftover rows so a downgrade to base cannot hit duplicate names."""
    from sqlalchemy import inspect, text

    names = set(inspect(engine).get_table_names())
    if {"tasks", "vms", "hosts"} <= names:
        with engine.begin() as c:
            c.execute(text("TRUNCATE tasks, vms, hosts RESTART IDENTITY CASCADE"))
