import pytest
from alembic import command
from sqlalchemy import inspect, text

from tests.dbutil import TEST_DB_URL, alembic_config


@pytest.fixture
def cfg(engine):
    c = alembic_config(TEST_DB_URL)
    command.upgrade(c, "head")
    try:
        yield c
    finally:
        command.upgrade(c, "head")


def test_downgrade_to_0005_drops_table(engine, cfg):
    command.downgrade(cfg, "0005")
    assert "host_metrics" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert "host_metrics" in insp.get_table_names()
    assert "ix_host_metrics_host_time" in {i["name"] for i in insp.get_indexes("host_metrics")}


def test_deleting_host_cascades_metrics(engine, session_factory):
    with engine.begin() as c:
        hid = c.execute(
            text("INSERT INTO hosts (name, libvirt_uri, status) VALUES ('h','u','ready') RETURNING id")
        ).scalar_one()
        c.execute(text("INSERT INTO host_metrics (host_id, cpu_pct, mem_used_mb) VALUES (:h, 5, 5)"), {"h": hid})
        c.execute(text("DELETE FROM hosts WHERE id = :h"), {"h": hid})
        assert c.execute(text("SELECT count(*) FROM host_metrics")).scalar_one() == 0
