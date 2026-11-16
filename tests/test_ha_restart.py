import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from minihci.config import Settings
from minihci.driver.base import DomainState
from minihci.driver.fake import FakeDriver
from minihci.models import HaRestart, Host, Task, Vm
from minihci.tasks.engine import enqueue
from minihci.tasks.executor import StepContext
from minihci.tasks.registry import REGISTRY
from tests.helpers import add_ready_host, drain, load_task
from tests.test_vm_power import URI, make_vm

URI_B = "qemu+ssh://b/system"
URI_C = "qemu+ssh://c/system"
STEPS = ["choose_host", "define_domain", "start_domain", "mark_running"]


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def dead_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def surv_id(session_factory, driver):
    return add_ready_host(session_factory, driver, name="b", uri=URI_B)


def set_status(session_factory, host_id, status):
    with session_factory() as s, s.begin():
        s.get(Host, host_id).status = status


def kill(session_factory, driver, host_id, uri=URI):
    driver.power_off_host(uri)
    set_status(session_factory, host_id, "dead")


def enqueue_restart(session_factory, vm, dead_id, age_s=10):
    failed_at = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    with session_factory() as s, s.begin():
        return enqueue(s, "vm.ha_restart", vm, {"dead_host_id": dead_id, "failed_at": failed_at}).id


def restart(session_factory, driver, vm, dead_id):
    tid = enqueue_restart(session_factory, vm, dead_id)
    drain(session_factory, driver)
    return load_task(session_factory, tid)


def vm_row(session_factory, vm):
    with session_factory() as s:
        return s.get(Vm, vm)


def ha_rows(session_factory):
    with session_factory() as s:
        return s.scalars(select(HaRestart)).all()


def steps_with_done(count):
    return [
        {
            "name": n,
            "status": "done" if i < count else "pending",
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        for i, n in enumerate(STEPS)
    ]


def test_restart_on_survivor(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    kill(session_factory, driver, dead_id)
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "succeeded", t.error
    assert driver.domain_state(URI_B, "web1") == DomainState.RUNNING
    row = vm_row(session_factory, vm)
    assert row.host_id == surv_id and row.actual_state == "running"
    (ha,) = ha_rows(session_factory)
    assert (ha.vm_id, ha.from_host_id, ha.to_host_id) == (vm, dead_id, surv_id)
    assert ha.running_at is not None and ha.recovery_ms >= 10_000


def test_suspect_host_is_not_fenced(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    driver.power_off_host(URI)
    set_status(session_factory, dead_id, "suspect")
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "rolled_back" and "not dead" in t.error
    assert driver.list_domains(URI_B) == {}
    assert ha_rows(session_factory) == []
    row = vm_row(session_factory, vm)
    assert row.actual_state == "running" and row.host_id == dead_id


def test_old_host_answers_running_is_refused(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    set_status(session_factory, dead_id, "dead")  # control plane says dead, but the host is up
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "rolled_back" and "still running" in t.error
    assert driver.list_domains(URI_B) == {}
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    row = vm_row(session_factory, vm)
    assert row.host_id == dead_id and row.actual_state == "pending_restart"  # known edge, D-028


def _resumed_after_choose_host(session_factory, driver, vm, dead_id, surv_id):
    tid = enqueue_restart(session_factory, vm, dead_id)
    with session_factory() as s, s.begin():
        s.add(HaRestart(task_id=tid, vm_id=vm, from_host_id=dead_id, to_host_id=surv_id, failed_at=datetime.now(UTC)))
        s.get(Task, tid).steps = steps_with_done(1)
    drain(session_factory, driver)
    return load_task(session_factory, tid)


def test_resume_rechecks_fence(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    set_status(session_factory, dead_id, "ready")  # host came back after choose_host was recorded
    t = _resumed_after_choose_host(session_factory, driver, vm, dead_id, surv_id)
    assert t.state == "rolled_back" and "not dead" in t.error
    assert driver.domain_state(URI_B, "web1") is None
    row = vm_row(session_factory, vm)
    assert row.actual_state == "running" and row.host_id == dead_id


def test_resume_fence_old_host_running(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    set_status(session_factory, dead_id, "dead")  # still dead, but the fake host answers RUNNING
    t = _resumed_after_choose_host(session_factory, driver, vm, dead_id, surv_id)
    assert t.state == "rolled_back" and "still running" in t.error
    assert driver.domain_state(URI_B, "web1") != DomainState.RUNNING
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING


def test_open_reclaim_on_old_host_fences_start(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    driver.destroy_domain(URI, "web1")
    set_status(session_factory, dead_id, "dead")
    with session_factory() as s, s.begin():
        enqueue(s, "host.reclaim", dead_id, {}).state = "running"  # open, but not claimable by the drain
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "rolled_back" and "reclaim" in t.error
    assert driver.domain_state(URI_B, "web1") != DomainState.RUNNING


def test_old_host_answers_shut_off_goes_ahead(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    driver.destroy_domain(URI, "web1")
    set_status(session_factory, dead_id, "dead")
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "succeeded", t.error
    assert driver.domain_state(URI_B, "web1") == DomainState.RUNNING


def test_no_capacity_rolls_back_to_pending_restart(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    kill(session_factory, driver, dead_id)
    with session_factory() as s, s.begin():
        s.get(Host, surv_id).mem_total_mb = 256
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "rolled_back"
    assert vm_row(session_factory, vm).actual_state == "pending_restart"


@pytest.mark.parametrize("executed", [1, 2, 3, 4])
@pytest.mark.parametrize("recorded_all", [True, False])
def test_resume_does_not_double_define_or_start(session_factory, driver, dead_id, surv_id, executed, recorded_all):
    """Run the first `executed` steps and record them done (or crash before the last was recorded), then re-run."""
    vm = make_vm(session_factory, driver, dead_id)
    kill(session_factory, driver, dead_id)
    tid = enqueue_restart(session_factory, vm, dead_id)
    ctx = StepContext(
        task_id=tid,
        target_id=vm,
        params=dict(load_task(session_factory, tid).params),
        driver=driver,
        session_factory=session_factory,
        settings=Settings(),
        log=logging.getLogger("t"),
    )
    for step in REGISTRY["vm.ha_restart"].steps[:executed]:
        step.do(ctx)
    with session_factory() as s, s.begin():
        s.get(Task, tid).steps = steps_with_done(executed if recorded_all else executed - 1)
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    assert driver.domain_state(URI_B, "web1") == DomainState.RUNNING
    assert driver.boot_count(URI_B, "web1") == 1
    assert len(ha_rows(session_factory)) == 1
    assert vm_row(session_factory, vm).host_id == surv_id


def test_resumed_task_reuses_recorded_host(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    kill(session_factory, driver, dead_id)
    third = add_ready_host(session_factory, driver, name="c", uri=URI_C)
    tid = enqueue_restart(session_factory, vm, dead_id)
    with session_factory() as s, s.begin():
        s.add(HaRestart(task_id=tid, vm_id=vm, from_host_id=dead_id, to_host_id=third, failed_at=datetime.now(UTC)))
        s.get(Task, tid).steps = steps_with_done(1)
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    assert driver.domain_state(URI_C, "web1") == DomainState.RUNNING
    assert driver.list_domains(URI_B) == {}
    assert vm_row(session_factory, vm).host_id == third


def test_start_failure_rolls_back_cleanly(session_factory, driver, dead_id, surv_id):
    vm = make_vm(session_factory, driver, dead_id)
    kill(session_factory, driver, dead_id)
    driver.fail = {"start_domain": 1}
    t = restart(session_factory, driver, vm, dead_id)
    assert t.state == "rolled_back"
    assert driver.list_domains(URI_B) == {}
    row = vm_row(session_factory, vm)
    assert row.actual_state == "pending_restart" and row.host_id == dead_id
    (ha,) = ha_rows(session_factory)
    assert ha.running_at is None
    t2 = restart(session_factory, driver, vm, dead_id)  # a later pass retries with a new task
    assert t2.state == "succeeded", t2.error
    assert vm_row(session_factory, vm).actual_state == "running"
