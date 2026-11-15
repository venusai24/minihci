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
