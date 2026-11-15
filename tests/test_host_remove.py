import pytest

from minihci.driver.fake import FakeDriver
from minihci.models import Host, Vm
from minihci.tasks.engine import enqueue
from minihci.tasks.executor import StepContext
from minihci.tasks.types import host_remove
from tests.helpers import add_ready_host, drain, load_task


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


def add_vm(session_factory, host_id, name, desired="running"):
    with session_factory() as s, s.begin():
        s.add(
            Vm(
                name=name,
                host_id=host_id,
                vcpus=1,
                mem_mb=512,
                image="x",
                disk_path=f"/d/{name}.qcow2",
                desired_state=desired,
                actual_state="absent" if desired == "absent" else "running",
            )
        )


def remove(session_factory, driver, host_id, prev="ready"):
    with session_factory() as s, s.begin():
        s.get(Host, host_id).status = "removing"
        tid = enqueue(s, "host.remove", host_id, {"prev_status": prev}).id
    drain(session_factory, driver)
    return load_task(session_factory, tid)


def status(session_factory, host_id):
    with session_factory() as s:
        return s.get(Host, host_id).status


def test_empty_host_removed_without_driver_changes(session_factory, driver, host_id):
    before = driver.list_domains("qemu+ssh://a/system")
    t = remove(session_factory, driver, host_id)
    assert t.state == "succeeded"
    assert [s["name"] for s in t.steps] == ["check_empty", "mark_removed"]
    assert status(session_factory, host_id) == "removed"
    assert driver.list_domains("qemu+ssh://a/system") == before


def test_host_with_live_vm_rolls_back(session_factory, driver, host_id):
    add_vm(session_factory, host_id, "web1")
    t = remove(session_factory, driver, host_id, prev="ready")
    assert t.state == "rolled_back"
    assert "web1" in t.error and "1 VM" in t.error
    assert status(session_factory, host_id) == "ready"


def test_only_absent_vm_does_not_block(session_factory, driver, host_id):
    add_vm(session_factory, host_id, "old", desired="absent")
    assert remove(session_factory, driver, host_id).state == "succeeded"
    assert status(session_factory, host_id) == "removed"


def test_mark_removed_is_idempotent(session_factory, driver, host_id):
    assert remove(session_factory, driver, host_id).state == "succeeded"
    ctx = StepContext(1, host_id, {}, driver, session_factory, None, None)
    host_remove.mark_removed(ctx)
    assert status(session_factory, host_id) == "removed"


def test_rollback_after_mark_removed_restores_host(session_factory, driver, host_id):
    from minihci.config import Settings
    from minihci.models import Task
    from minihci.tasks.executor import rollback_task
    from minihci.tasks.registry import REGISTRY
    from minihci.tasks.worker import claim_next

    with session_factory() as s, s.begin():
        s.get(Host, host_id).status = "removed"
        enqueue(s, "host.remove", host_id, {"prev_status": "ready"})
    tid = claim_next(session_factory).id
    rows = [
        {"name": "check_empty", "status": "done", "started_at": None, "finished_at": None, "error": None},
        {"name": "mark_removed", "status": "running", "started_at": None, "finished_at": None, "error": None},
    ]
    with session_factory() as s, s.begin():
        s.get(Task, tid).steps = rows
    state = rollback_task(tid, session_factory, driver, Settings(), REGISTRY, reason="crash")
    assert state.value == "rolled_back"
    assert status(session_factory, host_id) == "ready"
    assert load_task(session_factory, tid).steps[1]["status"] == "undone"


def test_undo_mark_removed_idempotent_and_noop(session_factory, driver, host_id):
    ctx = StepContext(1, host_id, {}, driver, session_factory, None, None)
    host_remove.undo_mark_removed(ctx)  # ready host: no-op
    assert status(session_factory, host_id) == "ready"
    with session_factory() as s, s.begin():
        s.get(Host, host_id).status = "removed"
    host_remove.undo_mark_removed(ctx)
    assert status(session_factory, host_id) == "removing"
    host_remove.undo_mark_removed(ctx)
    assert status(session_factory, host_id) == "removing"


def test_missing_prev_status_fails_and_leaves_removing(session_factory, driver, host_id):
    add_vm(session_factory, host_id, "web1")
    with session_factory() as s, s.begin():
        s.get(Host, host_id).status = "removing"
        tid = enqueue(s, "host.remove", host_id, {}).id
    drain(session_factory, driver)
    t = load_task(session_factory, tid)
    assert t.state == "failed"
    assert "prev_status" in t.error
    assert status(session_factory, host_id) == "removing"
