import logging
from datetime import datetime

import pytest
from sqlalchemy import select

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.ha.failover import failover_once
from minihci.models import HaRestart, Host, Task, Vm
from minihci.tasks.executor import StepContext
from minihci.tasks.types.vm_ha_restart import choose_host
from tests.helpers import add_ready_host, drain
from tests.test_vm_power import make_vm

URI_A = "qemu+ssh://a/system"
URI_B = "qemu+ssh://b/system"
S = Settings()


def _setup(session_factory, driver, survivor_mem):
    a = add_ready_host(session_factory, driver)
    b = add_ready_host(session_factory, driver, name="b", uri=URI_B)
    with session_factory() as s, s.begin():
        s.get(Host, b).mem_total_mb = survivor_mem
        h = s.get(Host, a)
        h.status, h.last_heartbeat = "dead", datetime(2026, 1, 1, 12, 0, 0)
    return a, b


def _ha_tasks(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(Task).where(Task.type == "vm.ha_restart").order_by(Task.id)))


def test_in_flight_restart_reserves_survivor_capacity(session_factory):
    driver = FakeDriver()
    a, b = _setup(session_factory, driver, 600)  # room for one 512 MB VM
    make_vm(session_factory, driver, a, name="v1")
    make_vm(session_factory, driver, a, name="v2")
    failover_once(session_factory, S)
    t1, t2 = _ha_tasks(session_factory)

    def ctx(t):
        return StepContext(t.id, t.target_id, t.params, driver, session_factory, S, logging.getLogger("t"))

    choose_host(ctx(t1))
    with pytest.raises(RuntimeError, match="no surviving host has capacity"):
        choose_host(ctx(t2))
    with session_factory() as s:
        assert list(s.scalars(select(HaRestart.to_host_id))) == [b]


def test_finished_restart_does_not_double_count(session_factory):
    driver = FakeDriver()
    a, b = _setup(session_factory, driver, 1100)  # room for two
    make_vm(session_factory, driver, a, name="v1")
    make_vm(session_factory, driver, a, name="v2")
    driver.destroy_domain(URI_A, "v1")  # old host reachable, copies off: fence passes
    driver.destroy_domain(URI_A, "v2")
    failover_once(session_factory, S)
    drain(session_factory, driver)
    with session_factory() as s:
        assert [v.host_id for v in s.scalars(select(Vm).order_by(Vm.id))] == [b, b]


def test_failing_restart_stops_after_max_attempts(session_factory):
    driver = FakeDriver()
    a, _ = _setup(session_factory, driver, 16384)
    make_vm(session_factory, driver, a, name="v1")
    driver.destroy_domain(URI_A, "v1")  # old host reachable, copy off: fence passes
    driver.fail["start_domain"] = 10_000
    for _ in range(8):
        failover_once(session_factory, S)
        drain(session_factory, driver)
    assert len(_ha_tasks(session_factory)) == S.ha_restart_max_attempts == 3
    with session_factory() as s:
        assert s.scalars(select(Vm.actual_state)).one() == "pending_restart"


def test_long_outage_recovery_ms_fits_the_column(session_factory):
    # failed_at is 10 months before "now": the elapsed milliseconds exceed int32 and must not break mark_running
    driver = FakeDriver()
    a, b = _setup(session_factory, driver, 16384)
    make_vm(session_factory, driver, a, name="v1")
    driver.destroy_domain(URI_A, "v1")
    failover_once(session_factory, S)
    drain(session_factory, driver)
    with session_factory() as s:
        assert s.scalars(select(Vm.host_id)).one() == b
        assert s.scalars(select(HaRestart.recovery_ms)).one() == 2**31 - 1
