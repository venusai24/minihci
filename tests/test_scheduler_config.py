import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from minihci.scheduler.config import load_config
from tests.dbutil import TEST_DB_URL, alembic_config


@pytest.fixture
def cfg(engine):
    c = alembic_config(TEST_DB_URL)
    command.upgrade(c, "head")
    try:
        yield c
    finally:
        command.upgrade(c, "head")


def test_upgrade_seeds_single_row_with_80(engine, cfg):
    command.downgrade(cfg, "0004")
    command.upgrade(cfg, "head")
    with engine.connect() as c:
        rows = c.execute(text("SELECT id, cpu_headroom_pct FROM scheduler_config")).all()
        updated = c.execute(text("SELECT updated_at FROM scheduler_config")).scalar_one()
    assert rows == [(1, 80)]
    assert updated is not None


def test_load_config_defaults_when_table_empty(engine, session_factory):
    with engine.begin() as c:
        c.execute(text("TRUNCATE scheduler_config"))
    with session_factory() as s:
        assert load_config(s).cpu_headroom_pct == 80


def test_load_config_reads_row(engine, session_factory):
    with engine.begin() as c:
        c.execute(text("TRUNCATE scheduler_config"))
        c.execute(text("INSERT INTO scheduler_config (id, cpu_headroom_pct) VALUES (1, 50)"))
    with session_factory() as s:
        assert load_config(s).cpu_headroom_pct == 50


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO scheduler_config (id, cpu_headroom_pct) VALUES (1, 0)",
        "INSERT INTO scheduler_config (id, cpu_headroom_pct) VALUES (1, 101)",
        "INSERT INTO scheduler_config (id, cpu_headroom_pct) VALUES (2, 50)",
    ],
)
def test_checks_reject_bad_rows(engine, session_factory, sql):
    with engine.begin() as c:
        c.execute(text("TRUNCATE scheduler_config"))
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text(sql))


def test_downgrade_to_0004_drops_table(engine, cfg):
    command.downgrade(cfg, "0004")
    assert "scheduler_config" not in inspect(engine).get_table_names()
