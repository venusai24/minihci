import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.base import DomainSpec, HostUnreachable
from minihci.driver.fake import FakeDriver
from minihci.logging import JsonFormatter, configure_logging
from minihci.models import Host, Task, Vm
from minihci.reconcile import reconcile_once
from tests.helpers import add_ready_host, drain

URI = "qemu+ssh://a/system"


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def logbuf():
    buf = io.StringIO()
    configure_logging("INFO", buf)
    yield buf
    logging.getLogger("minihci").handlers.clear()


def events(buf):
    out = []
    for line in buf.getvalue().splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if "event" in d:
            out.append(d)
    return out


def create_vm(session_factory, driver, host_id, name="web1", running=True, state=None, desired=None, uri=URI) -> int:
    path = f"/d/{name}.qcow2"
    driver.create_disk_overlay("/base.qcow2", path)
    driver.define_domain(uri, DomainSpec(name=name, vcpus=1, mem_mb=512, disk_path=path))
    if running:
        driver.start_domain(uri, name)
    with session_factory() as s, s.begin():
        vm = Vm(
            name=name,
            host_id=host_id,
            vcpus=1,
            mem_mb=512,
            image="x",
            disk_path=path,
            desired_state=desired or ("running" if running else "stopped"),
            actual_state=state or ("running" if running else "stopped"),
        )
        s.add(vm)
        s.flush()
        return vm.id


def actual(session_factory, vm_id):
    with session_factory() as s:
        return s.get(Vm, vm_id).actual_state


def run(session_factory, driver):
    return reconcile_once(session_factory, driver, Settings())


def test_m1_acceptance_vm_deleted_outside_is_detected(session_factory, driver, host_id, logbuf):
    client = TestClient(
        create_app(Settings(storage_root="/srv/test-images"), session_factory=session_factory, log_stream=logbuf)
    )
    r = client.post(
        "/vms",
        json={"name": "web1", "host_id": host_id, "vcpus": 1, "mem_mb": 512, "image": "/srv/minihci/base/a.qcow2"},
    )
    assert r.status_code == 202
    drain(session_factory, driver)
    vm_id = client.get("/vms").json()[0]["id"]
    assert actual(session_factory, vm_id) == "running"
    driver.destroy_domain(URI, "web1")
    driver.undefine_domain(URI, "web1")  # deleted outside MiniHCI
    report = run(session_factory, driver)
    assert actual(session_factory, vm_id) == "missing"
    assert report.missing == [vm_id]
    ev = [e for e in events(logbuf) if e["event"] == "vm_missing"]
    assert len(ev) == 1 and ev[0]["vm_id"] == vm_id and ev[0]["host_id"] == host_id
    assert ev[0]["expected"] == "running" and ev[0]["observed"] is None
    assert run(session_factory, driver).missing == []  # already missing: no repeat


def test_domain_shut_off_behind_our_back(session_factory, driver, host_id, logbuf):
    vm_id = create_vm(session_factory, driver, host_id)
    driver.destroy_domain(URI, "web1")
    report = run(session_factory, driver)
    assert actual(session_factory, vm_id) == "stopped"
    assert report.actual_drift == [vm_id]
    ev = [e for e in events(logbuf) if e["event"] == "actual_drift"]
    assert len(ev) == 1 and ev[0]["vm_id"] == vm_id
    assert (ev[0]["expected"], ev[0]["observed"]) == ("running", "stopped")
    assert run(session_factory, driver).actual_drift == []


def test_no_drift_reports_nothing(session_factory, driver, host_id, logbuf):
    create_vm(session_factory, driver, host_id)
    report = run(session_factory, driver)
    assert (report.missing, report.actual_drift, report.unknown, report.skipped) == ([], [], [], [])
    assert events(logbuf) == []


def test_unknown_domain_logged_not_touched(session_factory, driver, host_id, logbuf):
    driver.define_domain(URI, DomainSpec(name="foreign", vcpus=1, mem_mb=64, disk_path="/x"))
    before = json.dumps(driver._state, sort_keys=True)
    report = run(session_factory, driver)
    assert report.unknown == [(host_id, "foreign")]
    assert json.dumps(driver._state, sort_keys=True) == before
    assert [e["event"] for e in events(logbuf)] == ["unknown_domain"]


def test_vm_with_pending_task_skipped(session_factory, driver, host_id):
    from minihci.tasks.engine import enqueue

    vm_id = create_vm(session_factory, driver, host_id)
    driver.destroy_domain(URI, "web1")
    with session_factory() as s, s.begin():
        enqueue(s, "vm.start", vm_id, {})
    report = run(session_factory, driver)
    assert report.skipped == [vm_id]
    assert actual(session_factory, vm_id) == "running"


def test_vm_with_running_task_skipped(session_factory, driver, host_id):
    vm_id = create_vm(session_factory, driver, host_id)
    driver.destroy_domain(URI, "web1")
    with session_factory() as s, s.begin():
        s.add(Task(type="vm.stop", target_id=vm_id, state="running"))
    assert run(session_factory, driver).skipped == [vm_id]
    assert actual(session_factory, vm_id) == "running"


def test_vm_lock_held_elsewhere_skipped(session_factory, driver, host_id, engine):
    vm_id = create_vm(session_factory, driver, host_id)
    driver.destroy_domain(URI, "web1")
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(1, :i)"), {"i": vm_id})
        report = run(session_factory, driver)
        conn.rollback()
    assert report.skipped == [vm_id]
    assert actual(session_factory, vm_id) == "running"
    assert run(session_factory, driver).actual_drift == [vm_id]


def test_unreachable_host_logged_and_skipped(session_factory, driver, host_id, logbuf):
    vm_id = create_vm(session_factory, driver, host_id)
    del driver._state["hosts"][URI]  # host not in fake -> HostUnreachable
    report = run(session_factory, driver)
    assert report.missing == [] and actual(session_factory, vm_id) == "running"
    assert [e["event"] for e in events(logbuf)] == ["host_unreachable"]


def test_host_unreachable_exception_type(session_factory, host_id):
    class Boom(FakeDriver):
        def list_domains(self, uri):
            raise HostUnreachable("down")

    d = Boom()
    d.add_host(URI, 8, 16384)
    create_vm(session_factory, d, host_id)
    assert run(session_factory, d).missing == []


def test_domain_status_host_unreachable_skips_rest_of_host(session_factory, logbuf):
    class Flaky(FakeDriver):
        def domain_status(self, uri, name):
            if uri == URI:
                raise HostUnreachable("down")
            return super().domain_status(uri, name)

    d = Flaky()
    uri_b = "qemu+ssh://b/system"
    ha = add_ready_host(session_factory, d)
    hb = add_ready_host(session_factory, d, name="b", uri=uri_b)
    a1 = create_vm(session_factory, d, ha, name="a1")
    a2 = create_vm(session_factory, d, ha, name="a2")
    b1 = create_vm(session_factory, d, hb, name="b1", uri=uri_b)
    d.destroy_domain(URI, "a1")
    d.destroy_domain(URI, "a2")
    d.destroy_domain(uri_b, "b1")
    report = run(session_factory, d)
    assert report.skipped == [a1, a2]
    assert actual(session_factory, a1) == "running" and actual(session_factory, a2) == "running"
    assert report.actual_drift == [b1] and actual(session_factory, b1) == "stopped"
    ev = [e for e in events(logbuf) if e["event"] == "host_unreachable"]
    assert len(ev) == 1 and ev[0]["vm_id"] == a1 and ev[0]["host_id"] == ha


def test_domain_status_driver_error_skips_only_that_vm(session_factory, host_id, logbuf):
    d = FakeDriver(fail={"domain_status": 1})
    d.add_host(URI, 8, 16384)
    v1 = create_vm(session_factory, d, host_id, name="v1")
    v2 = create_vm(session_factory, d, host_id, name="v2")
    d.destroy_domain(URI, "v1")
    d.destroy_domain(URI, "v2")
    report = run(session_factory, d)
    assert report.skipped == [v1] and actual(session_factory, v1) == "running"
    assert report.actual_drift == [v2] and actual(session_factory, v2) == "stopped"
    ev = [e for e in events(logbuf) if e["event"] == "vm_check_failed"]
    assert len(ev) == 1 and ev[0]["vm_id"] == v1


def test_absent_vms_and_non_ready_hosts_ignored(session_factory, driver, host_id):
    absent = create_vm(session_factory, driver, host_id, name="gone", running=False, desired="absent", state="absent")
    driver.undefine_domain(URI, "gone")
    other_uri = "qemu+ssh://b/system"
    driver.add_host(other_uri, 4, 4096)
    with session_factory() as s, s.begin():
        h = Host(name="b", libvirt_uri=other_uri, status="registering")
        s.add(h)
        s.flush()
        vm = Vm(name="v2", host_id=h.id, vcpus=1, mem_mb=1, image="x", disk_path="/d/v2", actual_state="running")
        s.add(vm)
        s.flush()
        v2 = vm.id
    report = run(session_factory, driver)
    assert (report.missing, report.actual_drift, report.skipped) == ([], [], [])
    assert actual(session_factory, absent) == "absent"
    assert actual(session_factory, v2) == "running"


def test_json_formatter_merges_fields():
    rec = logging.LogRecord("minihci.x", logging.INFO, "f", 1, "hello", None, None)
    rec.fields = {"event": "e", "vm_id": 3}
    d = json.loads(JsonFormatter().format(rec))
    assert d["event"] == "e" and d["vm_id"] == 3 and d["msg"] == "hello" and "task_id" in d


def test_json_formatter_fields_cannot_clobber_core_keys():
    rec = logging.LogRecord("minihci.x", logging.INFO, "f", 1, "hello", None, None)
    rec.fields = {"msg": "evil", "level": "X"}
    d = json.loads(JsonFormatter().format(rec))
    assert d["msg"] == "hello" and d["level"] == "INFO"


def tasks_of(session_factory):
    with session_factory() as s:
        return [(t.type, t.target_id, t.params) for t in s.query(Task).order_by(Task.id)]


def test_corrective_start_queue_acceptance(session_factory, driver, host_id, logbuf):
    vm_id = create_vm(session_factory, driver, host_id, running=False, desired="running")
    report = run(session_factory, driver)
    assert len(report.corrective) == 1
    assert tasks_of(session_factory) == [("vm.start", vm_id, {})]
    ev = [e for e in events(logbuf) if e["event"] == "corrective_task"]
    assert len(ev) == 1 and ev[0]["task_id"] == report.corrective[0] and ev[0]["vm_id"] == vm_id
    drain(session_factory, driver)
    assert actual(session_factory, vm_id) == "running"
    again = run(session_factory, driver)
    assert (again.corrective, again.missing, again.actual_drift, again.skipped) == ([], [], [], [])


def test_corrective_stop_not_forced(session_factory, driver, host_id):
    vm_id = create_vm(session_factory, driver, host_id, running=True, desired="stopped")
    report = run(session_factory, driver)
    assert len(report.corrective) == 1
    assert tasks_of(session_factory) == [("vm.stop", vm_id, {"force": False})]
    drain(session_factory, driver)
    assert actual(session_factory, vm_id) == "stopped"


def test_no_duplicate_corrective_before_drain(session_factory, driver, host_id):
    create_vm(session_factory, driver, host_id, running=False, desired="running")
    run(session_factory, driver)
    second = run(session_factory, driver)
    assert second.corrective == [] and len(tasks_of(session_factory)) == 1


def test_missing_vm_gets_no_task(session_factory, driver, host_id):
    vm_id = create_vm(session_factory, driver, host_id)
    driver.destroy_domain(URI, "web1")
    driver.undefine_domain(URI, "web1")
    report = run(session_factory, driver)
    assert report.missing == [vm_id] and report.corrective == []
    assert tasks_of(session_factory) == []
    assert run(session_factory, driver).corrective == []


def test_reconcile_never_calls_mutating_driver_methods(session_factory, driver, host_id):
    create_vm(session_factory, driver, host_id, running=False, desired="running")
    create_vm(session_factory, driver, host_id, name="web2", running=True, desired="stopped")
    driver.fail.update({"start_domain": 5, "destroy_domain": 5, "shutdown_domain": 5})
    before = json.dumps(driver._state, sort_keys=True)
    report = run(session_factory, driver)
    assert len(report.corrective) == 2 and report.skipped == []
    assert json.dumps(driver._state, sort_keys=True) == before
    assert driver.fail == {"start_domain": 5, "destroy_domain": 5, "shutdown_domain": 5}


def test_unknown_domain_state_logged_and_skipped(session_factory, host_id, logbuf):
    from minihci.driver.base import DomainStatus

    class Weird(FakeDriver):
        def domain_status(self, uri, name):
            return DomainStatus("weird", None) if name == "v1" else super().domain_status(uri, name)

    d = Weird()
    d.add_host(URI, 8, 16384)
    v1 = create_vm(session_factory, d, host_id, name="v1")
    v2 = create_vm(session_factory, d, host_id, name="v2")
    d.destroy_domain(URI, "v2")
    report = run(session_factory, d)
    assert report.skipped == [v1] and actual(session_factory, v1) == "running"
    assert report.actual_drift == [v2]
    assert len(report.corrective) == 1  # only v2; nothing for the unknown-state VM
    ev = [e for e in events(logbuf) if e["event"] == "unknown_domain_state"]
    assert len(ev) == 1 and ev[0]["vm_id"] == v1 and ev[0]["state"] == "weird"


# ---- D-026 amendment 4: paused and crashed ----


def reason_of(session_factory, vm_id):
    with session_factory() as s:
        return s.get(Vm, vm_id).state_reason


def test_paused_ioerror_is_recorded_unhealthy_and_gets_no_start(session_factory, driver, host_id, logbuf):
    from minihci.driver.base import DomainState

    vm_id = create_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "ioerror")
    report = run(session_factory, driver)
    assert actual(session_factory, vm_id) == "paused" and reason_of(session_factory, vm_id) == "ioerror"
    assert report.actual_drift == [vm_id] and report.unhealthy == [vm_id]
    assert report.corrective == [] and tasks_of(session_factory) == []  # start cannot fix a paused domain
    errors = [e for e in events(logbuf) if e["event"] == "vm_unhealthy"]
    assert len(errors) == 1 and errors[0]["level"] == "ERROR" and errors[0]["reason"] == "ioerror"
    again = run(session_factory, driver)  # still unhealthy, but no new drift and no repeated error log
    assert again.unhealthy == [vm_id] and again.actual_drift == []
    assert len([e for e in events(logbuf) if e["event"] == "vm_unhealthy"]) == 1


def test_paused_by_user_is_recorded_but_not_unhealthy(session_factory, driver, host_id):
    from minihci.driver.base import DomainState

    vm_id = create_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "user")
    report = run(session_factory, driver)
    assert actual(session_factory, vm_id) == "paused" and reason_of(session_factory, vm_id) == "user"
    assert report.unhealthy == [] and report.corrective == []


def test_reason_change_while_paused_is_drift(session_factory, driver, host_id):
    from minihci.driver.base import DomainState

    vm_id = create_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "user")
    run(session_factory, driver)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "ioerror")
    report = run(session_factory, driver)
    assert report.actual_drift == [vm_id] and report.unhealthy == [vm_id]
    assert reason_of(session_factory, vm_id) == "ioerror"


def test_crashed_is_recorded_with_reason_and_not_restarted(session_factory, driver, host_id):
    from minihci.driver.base import DomainState

    vm_id = create_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.CRASHED, "panicked")
    report = run(session_factory, driver)
    assert actual(session_factory, vm_id) == "crashed" and reason_of(session_factory, vm_id) == "panicked"
    assert report.corrective == []


def test_reason_is_cleared_when_the_vm_recovers(session_factory, driver, host_id):
    from minihci.driver.base import DomainState

    vm_id = create_vm(session_factory, driver, host_id)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "ioerror")
    run(session_factory, driver)
    driver.set_domain_state(URI, "web1", DomainState.RUNNING, None)
    run(session_factory, driver)
    assert actual(session_factory, vm_id) == "running" and reason_of(session_factory, vm_id) is None


def test_paused_vm_that_should_be_stopped_gets_a_stop_task_that_destroys_it(session_factory, driver, host_id):
    from minihci.driver.base import DomainState

    vm_id = create_vm(session_factory, driver, host_id, desired="stopped", running=True)
    driver.set_domain_state(URI, "web1", DomainState.PAUSED, "user")
    report = run(session_factory, driver)
    assert len(report.corrective) == 1 and tasks_of(session_factory) == [("vm.stop", vm_id, {"force": False})]
    drain(session_factory, driver)
    assert driver.domain_state(URI, "web1") == DomainState.SHUT_OFF
    assert actual(session_factory, vm_id) == "stopped" and reason_of(session_factory, vm_id) is None
