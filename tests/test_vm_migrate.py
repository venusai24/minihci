import pytest
from sqlalchemy import select

from minihci.driver.base import DomainSpec, DomainState
from minihci.driver.fake import FakeDriver
from minihci.models import Host, Migration, Task, Vm
from minihci.tasks.engine import enqueue
from tests.helpers import add_ready_host, assert_db_matches_driver, drain, load_task
from tests.test_vm_power import URI, actual, make_vm, run

URI_B = "qemu+ssh://b/system"


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def dst_id(session_factory, driver):
    return add_ready_host(session_factory, driver, name="b", uri=URI_B)


def migrate(session_factory, driver, vm, src, dst):
    return run(session_factory, driver, "migrate", vm, {"src_host_id": src, "dst_host_id": dst})


def host_of(session_factory, vm):
    with session_factory() as s:
        return s.get(Vm, vm).host_id


def migration_row(session_factory, task_id):
    with session_factory() as s:
        return s.scalars(select(Migration).where(Migration.task_id == task_id)).one_or_none()


def test_migrate_success(session_factory, driver, host_id, dst_id):
    vm = make_vm(session_factory, driver, host_id)
    t = migrate(session_factory, driver, vm, host_id, dst_id)
    assert t.state == "succeeded"
    assert driver.domain_state(URI, "web1") is None
    assert driver.domain_state(URI_B, "web1") == DomainState.RUNNING
    assert host_of(session_factory, vm) == dst_id
    row = migration_row(session_factory, t.id)
    assert row.status == "succeeded" and row.finished_at is not None
    assert row.duration_ms >= 0 and row.downtime_ms == 30
    assert (row.vm_id, row.src_host_id, row.dst_host_id) == (vm, host_id, dst_id)
    assert_db_matches_driver(session_factory, driver)


def test_destination_full(session_factory, driver, host_id, dst_id):
    with session_factory() as s, s.begin():
        s.get(Host, dst_id).mem_total_mb = 256
    vm = make_vm(session_factory, driver, host_id)
    t = migrate(session_factory, driver, vm, host_id, dst_id)
    assert t.state == "rolled_back" and "capacity" in t.error
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert driver.domain_state(URI_B, "web1") is None
    assert host_of(session_factory, vm) == host_id
    row = migration_row(session_factory, t.id)
    assert row is None or row.status == "failed"
    assert_db_matches_driver(session_factory, driver)


def test_driver_failure_rolls_back(session_factory, driver, host_id, dst_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.fail = {"migrate_live": 1}
    t = migrate(session_factory, driver, vm, host_id, dst_id)
    assert t.state == "rolled_back"
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert host_of(session_factory, vm) == host_id
    row = migration_row(session_factory, t.id)
    assert row.status == "failed" and row.finished_at is not None
    assert actual(session_factory, vm) == "running"
    assert_db_matches_driver(session_factory, driver)


def test_resume_after_domain_already_moved(session_factory, driver, host_id, dst_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.migrate_live(URI, URI_B, "web1")
    with session_factory() as s, s.begin():
        tid = enqueue(s, "vm.migrate", vm, {"src_host_id": host_id, "dst_host_id": dst_id}).id
        task = s.get(Task, tid)
        task.steps = [
            {
                "name": n,
                "status": "done" if n == "check_destination" else "pending",
                "started_at": None,
                "finished_at": None,
                "error": None,
            }
            for n in ("check_destination", "migrate", "switch_host", "finish")
        ]
        s.add(Migration(task_id=tid, vm_id=vm, src_host_id=host_id, dst_host_id=dst_id))
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    row = migration_row(session_factory, tid)
    assert row.status == "succeeded" and row.duration_ms is None and row.downtime_ms is None
    assert host_of(session_factory, vm) == dst_id
    assert_db_matches_driver(session_factory, driver)


def test_source_stopped(session_factory, driver, host_id, dst_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    t = migrate(session_factory, driver, vm, host_id, dst_id)
    assert t.state == "rolled_back" and "not running" in t.error
    assert driver.domain_state(URI, "web1") == DomainState.SHUT_OFF
    assert host_of(session_factory, vm) == host_id
    assert_db_matches_driver(session_factory, driver)


def _sync(session_factory, driver, vm, src, dst, caplog):
    import logging

    from minihci.tasks.executor import StepContext
    from minihci.tasks.types.vm_migrate import sync_host

    with session_factory() as s, s.begin():
        tid = enqueue(s, "vm.migrate", vm, {"src_host_id": src, "dst_host_id": dst}).id
        s.add(Migration(task_id=tid, vm_id=vm, src_host_id=src, dst_host_id=dst))
    ctx = StepContext(
        task_id=tid,
        target_id=vm,
        params={"src_host_id": src, "dst_host_id": dst},
        driver=driver,
        session_factory=session_factory,
        settings=None,
        log=logging.getLogger("minihci.tasks"),
    )
    with caplog.at_level(logging.WARNING, logger="minihci.tasks"):
        sync_host(ctx)
    return tid


def _events(caplog, name):
    return [r for r in caplog.records if getattr(r, "fields", {}).get("event") == name]


def test_sync_host_domain_on_destination_only(session_factory, driver, host_id, dst_id, caplog):
    vm = make_vm(session_factory, driver, host_id)
    driver.migrate_live(URI, URI_B, "web1")
    tid = _sync(session_factory, driver, vm, host_id, dst_id, caplog)
    row = migration_row(session_factory, tid)
    assert row.status == "succeeded" and row.finished_at is not None
    assert host_of(session_factory, vm) == dst_id
    assert len(_events(caplog, "migrate_host_drift")) == 1


def test_sync_host_domain_on_both(session_factory, driver, host_id, dst_id, caplog):
    vm = make_vm(session_factory, driver, host_id)
    driver.define_domain(URI_B, DomainSpec(name="web1", vcpus=1, mem_mb=512, disk_path="/d/web1.qcow2"))
    tid = _sync(session_factory, driver, vm, host_id, dst_id, caplog)
    row = migration_row(session_factory, tid)
    assert row.status == "failed" and row.finished_at is not None
    assert host_of(session_factory, vm) == host_id
    (ev,) = _events(caplog, "migrate_placement_ambiguous")
    assert (ev.fields["on_src"], ev.fields["on_dst"]) == (True, True)
    assert not _events(caplog, "migrate_host_drift")


def test_sync_host_domain_on_neither(session_factory, driver, host_id, dst_id, caplog):
    vm = make_vm(session_factory, driver, host_id)
    driver.destroy_domain(URI, "web1")
    driver.undefine_domain(URI, "web1")
    tid = _sync(session_factory, driver, vm, host_id, dst_id, caplog)
    row = migration_row(session_factory, tid)
    assert row.status == "failed" and row.finished_at is not None
    assert host_of(session_factory, vm) == host_id
    (ev,) = _events(caplog, "migrate_placement_ambiguous")
    assert (ev.fields["on_src"], ev.fields["on_dst"]) == (False, False)
    assert not _events(caplog, "migrate_host_drift")


def test_sync_host_domain_on_source_only(session_factory, driver, host_id, dst_id, caplog):
    vm = make_vm(session_factory, driver, host_id)
    tid = _sync(session_factory, driver, vm, host_id, dst_id, caplog)
    row = migration_row(session_factory, tid)
    assert row.status == "failed" and row.finished_at is not None
    assert host_of(session_factory, vm) == host_id
    assert not _events(caplog, "migrate_host_drift")
    assert not _events(caplog, "migrate_placement_ambiguous")
