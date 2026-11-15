import pytest
from alembic import command
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from minihci.models import Host, Task, Vm
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
