import pytest
from alembic import command
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from tests.dbutil import TEST_DB_URL, alembic_config


def _host(c, name="h", site=None):
    cols, vals = "name, libvirt_uri", ":n, :n"
    if site is not None:
        cols, vals = cols + ", site", vals + ", :s"
    return c.execute(
        text(f"INSERT INTO hosts ({cols}) VALUES ({vals}) RETURNING id"), {"n": name, "s": site}
    ).scalar_one()


def _vm(c, host_id):
    return c.execute(
        text(
            "INSERT INTO vms (name, host_id, vcpus, mem_mb, image, disk_path) "
            "VALUES ('v', :h, 1, 512, 'i', '/d') RETURNING id"
        ),
        {"h": host_id},
    ).scalar_one()


def _task(c, vm_id):
    return c.execute(
        text("INSERT INTO tasks (type, target_id) VALUES ('vm.replicate', :v) RETURNING id"),
        {"v": vm_id},
    ).scalar_one()


def _replica(c, vm_id, task_id, host_id):
    c.execute(
        text(
            "INSERT INTO replicas (vm_id, task_id, host_id, mode, status, seq) "
            "VALUES (:v, :t, :h, 'full', 'running', 0)"
        ),
        {"v": vm_id, "t": task_id, "h": host_id},
    )


def test_host_site_defaults_primary(engine, session_factory):
    with engine.begin() as c:
        hid = _host(c)
        assert c.execute(text("SELECT site FROM hosts WHERE id=:i"), {"i": hid}).scalar_one() == "primary"


def test_host_site_check_rejects_other(engine, session_factory):
    with pytest.raises(IntegrityError), engine.begin() as c:
        _host(c, site="backup")


@pytest.mark.parametrize("interval,retention", [(29, 3), (30, 0), (30, 101), (86401, 3)])
def test_policy_limits(engine, session_factory, interval, retention):
    with engine.begin() as c:
        vid = _vm(c, _host(c))
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(
            text("INSERT INTO protection_policies (vm_id, interval_s, retention) VALUES (:v, :i, :r)"),
            {"v": vid, "i": interval, "r": retention},
        )
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO protection_policies (vm_id, interval_s, retention) VALUES (:v, 30, 100)"), {"v": vid}
        )
        assert c.execute(text("SELECT enabled FROM protection_policies")).scalar_one() is True


def test_replica_task_id_unique(engine, session_factory):
    with engine.begin() as c:
        hid = _host(c)
        vid = _vm(c, hid)
        tid = _task(c, vid)
        _replica(c, vid, tid, hid)
    with pytest.raises(IntegrityError), engine.begin() as c:
        _replica(c, vid, tid, hid)


def test_migration_downgrade_upgrade_roundtrip(engine, session_factory):
    cfg = alembic_config(TEST_DB_URL)
    try:
        command.downgrade(cfg, "0011")
        insp = inspect(engine)
        assert not {"protection_policies", "replicas", "dr_failovers"} & set(insp.get_table_names())
        assert "site" not in {c["name"] for c in insp.get_columns("hosts")}
    finally:
        command.upgrade(cfg, "head")
    insp = inspect(engine)
    assert {"protection_policies", "replicas", "dr_failovers"} <= set(insp.get_table_names())
    assert "ix_replicas_vm_status_time" in {i["name"] for i in insp.get_indexes("replicas")}
    assert "site" in {c["name"] for c in insp.get_columns("hosts")}
