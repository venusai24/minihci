import pytest
from alembic import command
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from minihci.models import HaRestart, Host, HostMetric, Migration, Task, Vm
from tests.dbutil import TEST_DB_URL, alembic_config, truncate_if_present


def make_host(s, name="h1", status="ready"):
    h = Host(name=name, libvirt_uri=f"qemu+ssh://{name}/system", status=status, cpu_total=8, mem_total_mb=16384)
    s.add(h)
    s.flush()
    return h


def test_tables_and_columns(engine):
    insp = inspect(engine)
    assert {"hosts", "vms", "tasks"} <= set(insp.get_table_names())
    assert {"id", "name", "host_id", "vcpus", "mem_mb", "image", "disk_path", "desired_state", "actual_state"} <= {
        c["name"] for c in insp.get_columns("vms")
    }
    tcols = {c["name"]: c for c in insp.get_columns("tasks")}
    assert {
        "id",
        "type",
        "target_id",
        "state",
        "steps",
        "params",
        "idempotency_key",
        "request_hash",
        "attempts",
        "created_at",
        "started_at",
        "finished_at",
        "error",
    } <= set(tcols)
    assert str(tcols["steps"]["type"]) == "JSONB"


def test_task_defaults(session_factory):
    with session_factory() as s:
        t = Task(type="t", target_id=1, params={"a": 1})
        s.add(t)
        s.commit()
        s.refresh(t)
        assert (t.state, t.steps, t.attempts, t.finished_at) == ("pending", [], 0, None)
        assert t.created_at is not None


def test_idempotency_key_unique_but_nullable(session_factory):
    with session_factory() as s:
        s.add_all([Task(type="t", target_id=1, params={}), Task(type="t", target_id=1, params={})])
        s.add(Task(type="t", target_id=1, params={}, idempotency_key="k1"))
        s.commit()
        s.add(Task(type="t", target_id=1, params={}, idempotency_key="k1"))
        with pytest.raises(IntegrityError, match="uq_tasks_idempotency_key"):
            s.commit()


def test_task_state_check_rejects_unknown(session_factory):
    with session_factory() as s:
        s.add(Task(type="t", target_id=1, params={}, state="exploded"))
        with pytest.raises(IntegrityError):
            s.commit()


def _vm(h, name="web1", path="/srv/web1.qcow2", **kw):
    return Vm(name=name, host_id=h.id, vcpus=1, mem_mb=512, image="/img/a.qcow2", disk_path=path, **kw)


def test_vm_defaults_and_unique_name(session_factory):
    with session_factory() as s:
        h = make_host(s)
        s.add(_vm(h))
        s.commit()
        vm = s.scalars(select(Vm)).one()
        assert (vm.desired_state, vm.actual_state) == ("running", "pending")
        s.add(_vm(h, path="/srv/x.qcow2"))
        with pytest.raises(IntegrityError, match="uq_vms_name_live"):
            s.commit()


def test_absent_vms_may_share_a_name(session_factory):
    with session_factory() as s:
        h = make_host(s)
        s.add_all([_vm(h, desired_state="absent"), _vm(h, desired_state="absent")])
        s.add(_vm(h))  # a live VM may reuse a name held only by absent rows
        s.commit()
        assert len(s.scalars(select(Vm)).all()) == 3
        s.add(_vm(h, desired_state="stopped"))
        with pytest.raises(IntegrityError, match="uq_vms_name_live"):
            s.commit()


def _host(name="h1", uri=None, status="ready"):
    return Host(name=name, libvirt_uri=uri or f"qemu+ssh://{name}/system", status=status)


def test_host_removed_and_removing_statuses_accepted(session_factory):
    with session_factory() as s:
        s.add_all([_host("a", status="removing"), _host("b", status="removed")])
        s.commit()
        s.add(_host("c", status="bogus"))
        with pytest.raises(IntegrityError, match="ck_hosts_status"):
            s.commit()


def test_removed_hosts_may_share_name_and_uri(session_factory):
    with session_factory() as s:
        s.add_all([_host("a", "u1", "removed"), _host("a", "u1", "removed")])
        s.add(_host("a", "u1", "ready"))  # a live host may reuse them
        s.commit()
        assert len(s.scalars(select(Host)).all()) == 3


def test_live_host_duplicate_name_or_uri_rejected(session_factory):
    with session_factory() as s:
        s.add(_host("a", "u1", "ready"))
        s.commit()
        s.add(_host("a", "u2", "removing"))
        with pytest.raises(IntegrityError, match="uq_hosts_name_live"):
            s.commit()
        s.rollback()
        s.add(_host("b", "u1", "failed"))
        with pytest.raises(IntegrityError, match="uq_hosts_uri_live"):
            s.commit()


def test_migration_roundtrip(engine):
    cfg = alembic_config(TEST_DB_URL)
    truncate_if_present(engine)
    command.downgrade(cfg, "base")
    assert "tasks" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    assert "tasks" in inspect(engine).get_table_names()


def test_vm_actual_state_missing_accepted_and_bogus_rejected(session_factory):
    with session_factory() as s:
        h = make_host(s)
        s.add(_vm(h, actual_state="missing"))
        s.commit()
        assert s.scalars(select(Vm)).one().actual_state == "missing"
        s.add(_vm(h, name="web2", path="/srv/w2.qcow2", actual_state="bogus"))
        with pytest.raises(IntegrityError, match="ck_vms_actual_state"):
            s.commit()


def test_host_metric_valid_row_has_server_default_time(session_factory):
    with session_factory() as s:
        h = make_host(s)
        s.add(HostMetric(host_id=h.id, cpu_pct=42.5, mem_used_mb=1024))
        s.commit()
        m = s.scalars(select(HostMetric)).one()
        assert m.collected_at is not None
        assert m.cpu_pct == 42.5


@pytest.mark.parametrize(
    ("cpu", "mem", "name"), [(101, 10, "ck_host_metrics_cpu_pct"), (10, -1, "ck_host_metrics_mem")]
)
def test_host_metric_checks(session_factory, cpu, mem, name):
    with session_factory() as s:
        h = make_host(s)
        s.add(HostMetric(host_id=h.id, cpu_pct=cpu, mem_used_mb=mem))
        with pytest.raises(IntegrityError, match=name):
            s.commit()


def _migration_parts(s):
    h1 = make_host(s, "h1")
    h2 = make_host(s, "h2")
    vm = _vm(h1)
    t = Task(type="vm.migrate", target_id=1)
    s.add_all([vm, t])
    s.flush()
    return h1, h2, vm, t


def _mig(h1, h2, vm, t, **kw):
    return Migration(task_id=t.id, vm_id=vm.id, src_host_id=h1.id, dst_host_id=h2.id, **kw)


def test_migration_valid_row_defaults(session_factory):
    with session_factory() as s:
        s.add(_mig(*_migration_parts(s)))
        s.commit()
        m = s.scalars(select(Migration)).one()
        assert m.status == "running"
        assert m.started_at is not None
        assert m.finished_at is None and m.duration_ms is None and m.downtime_ms is None


@pytest.mark.parametrize("kw", [{"status": "bogus"}, {"downtime_ms": -1}, {"duration_ms": -1}])
def test_migration_checks(session_factory, kw):
    with session_factory() as s:
        s.add(_mig(*_migration_parts(s), **kw))
        with pytest.raises(IntegrityError, match="ck_migrations"):
            s.commit()


def test_migration_task_id_unique(session_factory):
    with session_factory() as s:
        parts = _migration_parts(s)
        s.add(_mig(*parts))
        s.commit()
        s.add(_mig(*parts))
        with pytest.raises(IntegrityError, match="uq_migrations_task_id"):
            s.commit()


def test_downgrade_to_0006_drops_migrations(engine):
    cfg = alembic_config(TEST_DB_URL)
    command.upgrade(cfg, "head")
    try:
        command.downgrade(cfg, "0006")
        assert "migrations" not in inspect(engine).get_table_names()
    finally:
        command.upgrade(cfg, "head")
    assert "migrations" in inspect(engine).get_table_names()


def test_host_suspect_dead_accepted_and_missed_heartbeats_default(session_factory):
    with session_factory() as s:
        s.add_all([_host("a", status="suspect"), _host("b", status="dead")])
        s.commit()
        assert [h.missed_heartbeats for h in s.scalars(select(Host).order_by(Host.name))] == [0, 0]


def test_downgrade_to_0008_drops_missed_heartbeats_and_maps_suspect_dead_to_failed(engine):
    cfg = alembic_config(TEST_DB_URL)
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(text("TRUNCATE hosts CASCADE"))
    with engine.begin() as c:
        for name, status in [("h-suspect", "suspect"), ("h-dead", "dead"), ("h-ready", "ready")]:
            c.execute(
                text(
                    "insert into hosts (name, libvirt_uri, status, cpu_total, mem_total_mb) "
                    "values (:n, :u, :s, 8, 16384)"
                ),
                {"n": name, "u": f"qemu+ssh://{name}/system", "s": status},
            )
    try:
        command.downgrade(cfg, "0008")
        assert "missed_heartbeats" not in {c["name"] for c in inspect(engine).get_columns("hosts")}
        with engine.connect() as c:
            got = dict(c.execute(text("select name, status from hosts")).all())
        assert got == {"h-suspect": "failed", "h-dead": "failed", "h-ready": "ready"}
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as c:
            c.execute(text("TRUNCATE hosts CASCADE"))
    assert "missed_heartbeats" in {c["name"] for c in inspect(engine).get_columns("hosts")}


def test_vm_pending_restart_accepted_and_bogus_rejected(session_factory):
    with session_factory() as s:
        h = make_host(s)
        s.add(_vm(h, actual_state="pending_restart"))
        s.commit()
        assert s.scalars(select(Vm)).one().actual_state == "pending_restart"
        s.add(_vm(h, name="web2", path="/srv/w2.qcow2", actual_state="bogus"))
        with pytest.raises(IntegrityError, match="ck_vms_actual_state"):
            s.commit()


def _ha_parts(s):
    h1 = make_host(s, "h1")
    h2 = make_host(s, "h2")
    vm = _vm(h1)
    t = Task(type="vm.ha_restart", target_id=1)
    s.add_all([vm, t])
    s.flush()
    return h1, h2, vm, t


def _ha(h1, h2, vm, t, **kw):
    from datetime import UTC, datetime

    return HaRestart(task_id=t.id, vm_id=vm.id, from_host_id=h1.id, to_host_id=h2.id, failed_at=datetime.now(UTC), **kw)


def test_ha_restart_row_defaults_and_task_unique(session_factory):
    with session_factory() as s:
        parts = _ha_parts(s)
        s.add(_ha(*parts))
        s.commit()
        r = s.scalars(select(HaRestart)).one()
        assert r.created_at is not None and r.running_at is None and r.recovery_ms is None
        s.add(_ha(*parts))
        with pytest.raises(IntegrityError, match="uq_ha_restarts_task_id"):
            s.commit()


def test_downgrade_to_0009_drops_ha_restarts_and_maps_pending_restart_to_error(engine):
    cfg = alembic_config(TEST_DB_URL)
    command.upgrade(cfg, "head")
    with engine.begin() as c:
        c.execute(text("TRUNCATE tasks, vms, hosts CASCADE"))
        hid = c.execute(text("INSERT INTO hosts (name, libvirt_uri) VALUES ('h', 'u') RETURNING id")).scalar_one()
        c.execute(
            text(
                "INSERT INTO vms (name, host_id, vcpus, mem_mb, image, disk_path, desired_state, actual_state) "
                "VALUES ('v', :h, 1, 512, 'i', '/d', 'running', 'pending_restart')"
            ),
            {"h": hid},
        )
    try:
        command.downgrade(cfg, "0009")
        assert "ha_restarts" not in inspect(engine).get_table_names()
        with engine.connect() as c:
            assert c.execute(text("SELECT actual_state FROM vms")).scalar_one() == "error"
    finally:
        command.upgrade(cfg, "head")
        with engine.begin() as c:
            c.execute(text("TRUNCATE tasks, vms, hosts CASCADE"))
    assert "ha_restarts" in inspect(engine).get_table_names()
