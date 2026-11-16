import pytest
from alembic import command
from sqlalchemy import text

from minihci.db import make_engine, make_session_factory
from tests.dbutil import TEST_DB_URL, alembic_config, truncate_if_present


@pytest.fixture(scope="session")
def engine():
    eng = make_engine(TEST_DB_URL)
    try:
        with eng.connect():
            pass
    except Exception as e:  # noqa: BLE001
        pytest.exit(f"Test Postgres unreachable at {TEST_DB_URL}; run scripts/dev-db.sh ({e})", returncode=2)
    truncate_if_present(eng)
    command.downgrade(alembic_config(TEST_DB_URL), "base")
    command.upgrade(alembic_config(TEST_DB_URL), "head")
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    with engine.begin() as c:
        c.execute(text("TRUNCATE tasks, vms, hosts, scheduler_config, host_metrics RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)
