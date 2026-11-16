import logging
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from minihci.config import Settings
from minihci.driver.base import DomainSpec, DomainState
from minihci.driver.fake import FakeDriver
from minihci.ha.heartbeat import heartbeat_once
from minihci.models import Host, Task, Vm
from minihci.tasks.executor import StepContext
from minihci.tasks.locks import LOCK_NAMESPACE
from minihci.tasks.types.host_reclaim import mark_ready, remove_stale
from tests.helpers import add_ready_host, drain
from tests.test_vm_power import URI, make_vm

URI_B = "qemu+ssh://b/system"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
S = Settings(heartbeat_ping_timeout_s=0.5)


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def a_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def b_id(session_factory, driver):
    return add_ready_host(session_factory, driver, name="b", uri=URI_B)


def status(session_factory, hid):
    with session_factory() as s:
        return s.get(Host, hid).status


def reclaim_tasks(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(Task).where(Task.type == "host.reclaim").order_by(Task.id)))


def move_to_b(session_factory, driver, vm_id, b_id):
    """The VM was restarted on b: row points at b, a still holds a stale copy."""
    with session_factory() as s, s.begin():
        vm = s.get(Vm, vm_id)
        vm.host_id = b_id
        driver.define_domain(URI_B, DomainSpec(name=vm.name, vcpus=1, mem_mb=512, disk_path=vm.disk_path))
        driver.start_domain(URI_B, vm.name)


def return_dead_host(session_factory, driver, a_id):
    with session_factory() as s, s.begin():
        s.get(Host, a_id).status = "dead"
    driver.power_on_host(URI)
    heartbeat_once(session_factory, driver, S, now=T0)


def test_running_stale_copy_removed_then_ready(session_factory, driver, a_id, b_id, caplog):
    vm = make_vm(session_factory, driver, a_id)
    move_to_b(session_factory, driver, vm, b_id)
    return_dead_host(session_factory, driver, a_id)
    assert status(session_factory, a_id) == "dead"  # not ready until reclaimed
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    with caplog.at_level(logging.INFO):
        drain(session_factory, driver)
    assert driver.domain_state(URI, "web1") is None
    assert driver.domain_state(URI_B, "web1") == DomainState.RUNNING
    assert status(session_factory, a_id) == "ready"
    (t,) = reclaim_tasks(session_factory)
    assert t.state == "succeeded" and t.target_id == a_id
    ev = [r for r in caplog.records if getattr(r, "fields", {}).get("event") == "stale_copy_removed"]
    assert len(ev) == 1 and ev[0].fields["vm"] == "web1"


def test_shut_off_stale_copy_removed_and_undefined_is_fine(session_factory, driver, a_id, b_id):
    v1 = make_vm(session_factory, driver, a_id, running=False, name="old")
    v2 = make_vm(session_factory, driver, a_id, name="lost")
    for v in (v1, v2):
        move_to_b(session_factory, driver, v, b_id)
    driver.destroy_domain(URI, "lost")
    driver.undefine_domain(URI, "lost")  # never defined on the returning host
    return_dead_host(session_factory, driver, a_id)
    drain(session_factory, driver)
    assert driver.list_domains(URI) == {}
    assert status(session_factory, a_id) == "ready"


def test_pending_restart_vm_kept(session_factory, driver, a_id, b_id):
    vm = make_vm(session_factory, driver, a_id)
    with session_factory() as s, s.begin():
        s.get(Vm, vm).actual_state = "pending_restart"
    return_dead_host(session_factory, driver, a_id)
    drain(session_factory, driver)
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert status(session_factory, a_id) == "ready"


def test_absent_vm_domain_removed(session_factory, driver, a_id, b_id):
    vm = make_vm(session_factory, driver, a_id)
    with session_factory() as s, s.begin():
        v = s.get(Vm, vm)
        v.desired_state = v.actual_state = "absent"
    return_dead_host(session_factory, driver, a_id)
    drain(session_factory, driver)
    assert driver.domain_state(URI, "web1") is None


def test_one_open_reclaim_at_most(session_factory, driver, a_id, b_id):
    return_dead_host(session_factory, driver, a_id)
    heartbeat_once(session_factory, driver, S, now=T0 + timedelta(seconds=5))
    assert len(reclaim_tasks(session_factory)) == 1
    drain(session_factory, driver)
    assert status(session_factory, a_id) == "ready"


def test_failed_reclaim_keeps_host_dead_and_retries(session_factory, driver, a_id, b_id):
    vm = make_vm(session_factory, driver, a_id)
    move_to_b(session_factory, driver, vm, b_id)
    return_dead_host(session_factory, driver, a_id)
    real = driver.destroy_domain

    def boom(uri, name):
        raise RuntimeError("nope")

    driver.destroy_domain = boom
    drain(session_factory, driver)
    assert status(session_factory, a_id) == "dead"
    assert reclaim_tasks(session_factory)[0].state in ("failed", "rolled_back")
    driver.destroy_domain = real
    heartbeat_once(session_factory, driver, S, now=T0 + timedelta(seconds=5))
    drain(session_factory, driver)
    assert status(session_factory, a_id) == "ready"
    assert driver.domain_state(URI, "web1") is None


def make_ctx(session_factory, driver, host_id):
    return StepContext(
        task_id=0,
        target_id=host_id,
        params={},
        driver=driver,
        session_factory=session_factory,
        settings=S,
        log=logging.getLogger("test"),
    )


def run_blocked(session_factory, driver, a_id, vm_id, while_locked):
    """Hold the VM lock in another connection, run remove_stale in a thread, check it waits, then release."""
    ctx = make_ctx(session_factory, driver, a_id)
    t = threading.Thread(target=remove_stale, args=(ctx,))
    with session_factory() as s, s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": LOCK_NAMESPACE["vm"], "id": vm_id})
        t.start()
        t.join(0.5)
        assert t.is_alive(), "remove_stale did not wait for the VM lock"
        while_locked(s)
    t.join(10)
    assert not t.is_alive()


def test_remove_waits_for_vm_lock_then_sees_moved_row(session_factory, driver, a_id, b_id):
    vm = make_vm(session_factory, driver, a_id)
    run_blocked(session_factory, driver, a_id, vm, lambda s: s.get(Vm, vm).__setattr__("host_id", b_id))
    assert driver.domain_state(URI, "web1") is None


def test_row_pointing_at_this_host_still_takes_lock_and_is_kept(session_factory, driver, a_id, b_id):
    vm = make_vm(session_factory, driver, a_id)
    run_blocked(session_factory, driver, a_id, vm, lambda s: None)
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING


def test_mark_ready_only_with_zero_missed(session_factory, driver, a_id):
    with session_factory() as s, s.begin():
        h = s.get(Host, a_id)
        h.status, h.missed_heartbeats = "dead", 2
    mark_ready(make_ctx(session_factory, driver, a_id))
    assert status(session_factory, a_id) == "dead"
    with session_factory() as s, s.begin():
        s.get(Host, a_id).missed_heartbeats = 0
    mark_ready(make_ctx(session_factory, driver, a_id))
    assert status(session_factory, a_id) == "ready"
