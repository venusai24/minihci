import pytest

from minihci.driver.fake import FakeDriver
from minihci.models import Vm
from tests.helpers import add_ready_host, assert_db_matches_driver
from tests.test_vm_power import URI, actual, make_vm, run


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


def gone(driver, name="web1"):
    return driver.domain_state(URI, name) is None and not driver.disk_exists(f"/d/{name}.qcow2")


@pytest.mark.parametrize("running", [True, False])
def test_delete_vm(session_factory, driver, host_id, running):
    vm = make_vm(session_factory, driver, host_id, running=running)
    t = run(session_factory, driver, "delete", vm)
    assert t.state == "succeeded"
    assert gone(driver)
    with session_factory() as s:
        row = s.get(Vm, vm)
        assert (row.desired_state, row.actual_state) == ("absent", "absent")
    assert_db_matches_driver(session_factory, driver)


def test_half_deleted_succeeds(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    driver.undefine_domain(URI, "web1")
    assert run(session_factory, driver, "delete", vm).state == "succeeded"
    assert gone(driver) and actual(session_factory, vm) == "absent"


def test_delete_disk_failure_then_retry(session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.fail = {"delete_disk": 1}
    t = run(session_factory, driver, "delete", vm)
    assert t.state == "rolled_back"
    assert actual(session_factory, vm) == "error"
    assert driver.domain_state(URI, "web1") is None and driver.disk_exists("/d/web1.qcow2")
    assert run(session_factory, driver, "delete", vm).state == "succeeded"
    assert gone(driver) and actual(session_factory, vm) == "absent"
    assert_db_matches_driver(session_factory, driver)


def test_name_reusable_after_delete(session_factory, driver, host_id):
    old = make_vm(session_factory, driver, host_id)
    assert run(session_factory, driver, "delete", old).state == "succeeded"
    new = make_vm(session_factory, driver, host_id)
    assert new != old
    assert actual(session_factory, new) == "running"
    assert actual(session_factory, old) == "absent"
    assert_db_matches_driver(session_factory, driver)
