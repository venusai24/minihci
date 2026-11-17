"""M5 acceptance: site loss, failover to the DR site within the RPO target (FR-5.3, NFR-8).

FakeDriver plus the real loops called by hand (replicate_once, the worker via drain, heartbeat_once, failover_once,
the API and the vm.replicate / vm.dr_failover task code). Nothing of them is mocked; only the clock is faked.
"""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from minihci.app import create_app
from minihci.config import Settings
from minihci.dr.chain import RPO_SLACK_S
from minihci.dr.scheduler import replicate_once
from minihci.driver.base import DomainState
from minihci.driver.fake import FakeDriver
from minihci.ha.failover import failover_once
from minihci.ha.heartbeat import heartbeat_once
from minihci.models import DrFailover, Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.types import vm_replicate as vr
from tests.helpers import add_ready_host, drain, load_task
from tests.test_vm_migrate import URI_B
from tests.test_vm_power import URI, make_vm

URI_DR = "qemu:///system"
BASE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
INTERVAL = 60
S = Settings()


@pytest.fixture
def clock(monkeypatch):
    """Fake clock: vm.replicate stamps snapshot_time with datetime.now(UTC); make that the test's clock."""
    now = [BASE]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0] if tz is not None else now[0].replace(tzinfo=None)

    monkeypatch.setattr(vr, "datetime", FakeDateTime)
    return now


def test_site_loss_failover_within_rpo(session_factory, clock):
    driver = FakeDriver()
    client = TestClient(create_app(S, session_factory=session_factory))
    a = add_ready_host(session_factory, driver, name="a", uri=URI)
    b = add_ready_host(session_factory, driver, name="b", uri=URI_B)
    dr = add_ready_host(session_factory, driver, name="dr", uri=URI_DR)
    with session_factory() as s, s.begin():
        s.get(Host, dr).site = "dr"
    vm = make_vm(session_factory, driver, a)
    with session_factory() as s, s.begin():
        s.add(ProtectionPolicy(vm_id=vm, interval_s=INTERVAL, retention=3, enabled=True))
        disk = s.get(Vm, vm).disk_path

    # 4 replication intervals with the guest writing in between
    for k in range(4):
        t = BASE + timedelta(seconds=INTERVAL * k)
        clock[0] = t
        driver.write_blocks(URI, "web1", [k, 10 + k])
        heartbeat_once(session_factory, driver, S, now=t)
        assert len(replicate_once(session_factory, S, now=t)) == 1, k
        drain(session_factory, driver)
        assert replicate_once(session_factory, S, now=t + timedelta(seconds=INTERVAL // 2)) == []  # not due yet
    expected = driver.read_disk(disk)
    with session_factory() as s:
        reps = list(s.scalars(select(Replica).where(Replica.vm_id == vm).order_by(Replica.id)))
    assert len(reps) == 4
    assert all(r.status in ("succeeded", "pruned") for r in reps)
    last = reps[-1]
    assert last.status == "succeeded" and last.snapshot_time == BASE + timedelta(seconds=INTERVAL * 3)
    assert driver.read_disk(last.dr_path) == expected

    # writes after the last replica: lost by design (D-037)
    t3 = BASE + timedelta(seconds=INTERVAL * 3)
    driver.write_blocks(URI, "web1", [1, 50])
    assert driver.read_disk(disk) != expected
    heartbeat_once(session_factory, driver, S, now=t3 + timedelta(seconds=10))  # the last good heartbeat

    # site loss: both primary hosts go dark; real heartbeats mark them dead (3 misses, >= 30 s since the last good one)
    driver.power_off_host(URI)
    driver.power_off_host(URI_B)
    for dt in (15, 20, 25, 45):
        heartbeat_once(session_factory, driver, S, now=t3 + timedelta(seconds=dt))
    with session_factory() as s:
        assert {h.id: h.status for h in s.scalars(select(Host))} == {a: "dead", b: "dead", dr: "ready"}

    # HA cannot help: no primary host survives and DR-site hosts are not HA targets
    failover_once(session_factory, S)
    with session_factory() as s:
        assert s.scalars(select(Task).where(Task.type == "vm.ha_restart")).all() == []
        assert s.get(Vm, vm).actual_state == "pending_restart"

    r = client.post(f"/vms/{vm}/failover", headers={"Idempotency-Key": "site-loss"})
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"

    with session_factory() as s:
        row = s.scalars(select(DrFailover).where(DrFailover.task_id == tid)).one()
        v = s.get(Vm, vm)
        assert row.replica_id == last.id
        assert (v.host_id, v.disk_path, v.actual_state) == (dr, row.disk_path, "running")
        assert s.get(ProtectionPolicy, vm).enabled is False
    assert driver.domain_state(URI_DR, "web1") == DomainState.RUNNING
    assert driver.read_disk(row.disk_path) == expected
    assert row.data_loss_s == pytest.approx(10.0)
    assert row.data_loss_s <= INTERVAL + RPO_SLACK_S

    # the HA pass and the replication scheduler leave the failed-over VM alone
    failover_once(session_factory, S)
    replicated = replicate_once(session_factory, S, now=t3 + timedelta(hours=1))
    with session_factory() as s:
        assert s.scalars(select(Task).where(Task.type == "vm.ha_restart")).all() == []
        assert s.get(Vm, vm).actual_state == "running"
    assert replicated == []
