import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.dbutil import TEST_DB_URL, alembic_config


def _indexes(conn):
    return {i["name"] for i in inspect(conn).get_indexes("vms")}


def _uniques(conn):
    return {u["name"] for u in inspect(conn).get_unique_constraints("vms")}


def _insert_vm(conn, name, host_id):
    return conn.execute(
        text(
            "INSERT INTO vms (name, host_id, vcpus, mem_mb, image, disk_path, desired_state, actual_state) "
            "VALUES (:n, :h, 1, 512, 'i', '/d', 'absent', 'absent') RETURNING id"
        ),
        {"n": name, "h": host_id},
    ).scalar_one()


@pytest.fixture
def at_head(engine):
    cfg = alembic_config(TEST_DB_URL)
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(text("TRUNCATE tasks, vms, hosts RESTART IDENTITY CASCADE"))
    try:
        yield cfg
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as c:  # leave no duplicate names behind: the session fixture downgrades to base
            c.execute(text("TRUNCATE tasks, vms, hosts RESTART IDENTITY CASCADE"))


def test_downgrade_refuses_duplicate_absent_names_and_loses_nothing(engine, at_head):
    cfg = at_head
    with engine.begin() as c:
        hid = c.execute(text("INSERT INTO hosts (name, libvirt_uri) VALUES ('h', 'u') RETURNING id")).scalar_one()
        a = _insert_vm(c, "dup", hid)
        b = _insert_vm(c, "dup", hid)
    with engine.connect() as c:
        before = c.execute(text("SELECT * FROM vms ORDER BY id")).all()

    with pytest.raises(IntegrityError):
        command.downgrade(cfg, "0001")
    with engine.connect() as c:
        assert c.execute(text("SELECT * FROM vms ORDER BY id")).all() == before
        assert len(before) == 2
        assert "uq_vms_name_live" in _indexes(c)

    with engine.begin() as c:
        c.execute(text("DELETE FROM vms WHERE id = :i"), {"i": b})
    command.downgrade(cfg, "0001")
    with engine.connect() as c:
        assert "vms_name_key" in _uniques(c)
        assert "uq_vms_name_live" not in _indexes(c)

    command.upgrade(cfg, "head")
    with engine.begin() as c:
        assert "uq_vms_name_live" in _indexes(c)
        _insert_vm(c, "dup", hid)
        assert c.execute(text("SELECT count(*) FROM vms WHERE name='dup'")).scalar_one() == 2
    assert a
