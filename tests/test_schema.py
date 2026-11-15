import pytest
from alembic import command
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from minihci.models import Host, Task, Vm
from tests.dbutil import TEST_DB_URL, alembic_config


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


def test_vm_defaults_and_unique_name(session_factory):
    with session_factory() as s:
        h = make_host(s)
        s.add(Vm(name="web1", host_id=h.id, vcpus=1, mem_mb=512, image="/img/a.qcow2", disk_path="/srv/web1.qcow2"))
        s.commit()
        vm = s.scalars(select(Vm)).one()
        assert (vm.desired_state, vm.actual_state) == ("running", "pending")
        s.add(Vm(name="web1", host_id=h.id, vcpus=1, mem_mb=512, image="/img/a.qcow2", disk_path="/srv/x.qcow2"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_migration_roundtrip(engine):
    cfg = alembic_config(TEST_DB_URL)
    command.downgrade(cfg, "base")
    assert "tasks" not in inspect(engine).get_table_names()
    command.upgrade(cfg, "head")
    assert "tasks" in inspect(engine).get_table_names()
