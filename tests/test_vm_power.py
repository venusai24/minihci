import pytest

from minihci.config import Settings
from minihci.driver.base import DomainSpec, DomainState
from minihci.driver.fake import FakeDriver
from minihci.models import Vm
from minihci.tasks.engine import enqueue
from tests.helpers import add_ready_host, assert_db_matches_driver, drain, load_task

URI = "qemu+ssh://a/system"
MUTATING = ["start_domain", "shutdown_domain", "destroy_domain", "reboot_domain"]


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


def make_vm(session_factory, driver, host_id, running=True, name="web1") -> int:
    path = f"/d/{name}.qcow2"
    driver.create_disk_overlay("/base.qcow2", path)
    driver.define_domain(URI, DomainSpec(name=name, vcpus=1, mem_mb=512, disk_path=path))
    if running:
        driver.start_domain(URI, name)
    with session_factory() as s, s.begin():
        vm = Vm(
            name=name,
            host_id=host_id,
            vcpus=1,
            mem_mb=512,
            image="x",
            disk_path=path,
            desired_state="running" if running else "stopped",
            actual_state="running" if running else "stopped",
        )
        s.add(vm)
        s.flush()
        return vm.id


def run(session_factory, driver, op, vm_id, params=None, settings=None):
    with session_factory() as s, s.begin():
        tid = enqueue(s, f"vm.{op}", vm_id, params or {}).id
    drain(session_factory, driver, settings=settings)
    return load_task(session_factory, tid)


def actual(session_factory, vm_id):
    with session_factory() as s:
        return s.get(Vm, vm_id).actual_state


def test_start_stopped_vm(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    t = run(session_factory, driver, "start", vm)
    assert t.state == "succeeded"
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert actual(session_factory, vm) == "running"
    assert_db_matches_driver(session_factory, driver)


def test_start_running_is_noop(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.fail = {m: 5 for m in MUTATING}
    t = run(session_factory, driver, "start", vm)
    assert t.state == "succeeded"
    assert driver.boot_count(URI, "web1") == 1


def test_start_undefined_domain_fails(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    driver.undefine_domain(URI, "web1")
    t = run(session_factory, driver, "start", vm)
    assert t.state == "rolled_back" and "domain not defined" in t.error
    assert actual(session_factory, vm) == "error"


def test_stop_undefined_domain_fails(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    driver.undefine_domain(URI, "web1")
    for params in ({}, {"force": True}):
        t = run(session_factory, driver, "stop", vm, params)
        assert t.state == "rolled_back" and "domain not defined" in t.error
    assert actual(session_factory, vm) == "error"


def test_graceful_stop(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    t = run(session_factory, driver, "stop", vm, {"force": False})
    assert t.state == "succeeded"
    assert driver.domain_state(URI, "web1") == DomainState.SHUT_OFF
    assert actual(session_factory, vm) == "stopped"
    assert_db_matches_driver(session_factory, driver)


def test_force_stop_uses_destroy(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.fail = {"shutdown_domain": 5}
    t = run(session_factory, driver, "stop", vm, {"force": True})
    assert t.state == "succeeded"
    assert driver.domain_state(URI, "web1") == DomainState.SHUT_OFF
    assert actual(session_factory, vm) == "stopped"


def test_stop_stopped_is_noop(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    driver.fail = {m: 5 for m in MUTATING}
    for params in ({}, {"force": True}):
        assert run(session_factory, driver, "stop", vm, params).state == "succeeded"
    assert actual(session_factory, vm) == "stopped"


def test_reboot_running_bumps_boot_count(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    t = run(session_factory, driver, "reboot", vm)
    assert t.state == "succeeded"
    assert driver.boot_count(URI, "web1") == 2
    assert actual(session_factory, vm) == "running"


def test_reboot_stopped_starts_it(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    assert run(session_factory, driver, "reboot", vm).state == "succeeded"
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert actual(session_factory, vm) == "running"


class SlowShutdownDriver(FakeDriver):
    def shutdown_domain(self, uri, name):  # guest ignores the request
        pass


def test_graceful_timeout_rolls_back_and_stays_running(session_factory, host_id):
    driver = SlowShutdownDriver()
    driver.add_host(URI, 8, 16384)
    vm = make_vm(session_factory, driver, host_id)
    t = run(session_factory, driver, "stop", vm, {"force": False}, settings=Settings(vm_shutdown_timeout_s=0.5))
    assert t.state == "rolled_back"
    assert "graceful shutdown timed out; retry with force" in t.error
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert actual(session_factory, vm) == "running"


def test_rollback_never_touches_absent_row(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    with session_factory() as s, s.begin():
        s.get(Vm, vm).actual_state = "absent"
    driver.undefine_domain(URI, "web1")
    run(session_factory, driver, "start", vm)
    assert actual(session_factory, vm) == "absent"


def test_stop_destroys_a_paused_vm_and_clears_the_reason(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "ioerror")
    with session_factory() as s, s.begin():
        row = s.get(Vm, vm)
        row.actual_state, row.state_reason = "paused", "ioerror"
    t = run(session_factory, driver, "stop", vm)
    assert t.state == "succeeded"
    assert driver.domain_state(URI, "web1") == DomainState.SHUT_OFF
    with session_factory() as s:
        row = s.get(Vm, vm)
        assert (row.actual_state, row.state_reason) == ("stopped", None)


def test_start_on_a_paused_vm_fails_and_records_paused_not_running(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "user")
    t = run(session_factory, driver, "start", vm)
    assert t.state != "succeeded"
    with session_factory() as s:
        row = s.get(Vm, vm)
        assert (row.actual_state, row.state_reason) == ("paused", "user")
