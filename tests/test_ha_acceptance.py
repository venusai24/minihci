from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from minihci.config import Settings
from minihci.driver.base import DomainSpec, DomainState
from minihci.driver.fake import FakeDriver
from minihci.ha.failover import failover_once
from minihci.ha.heartbeat import heartbeat_once
from minihci.models import HaRestart, Host, Vm
from tests.helpers import add_ready_host, assert_db_matches_driver, drain
from tests.test_vm_power import URI, make_vm

URI_B = "qemu+ssh://b/system"
URI_C = "qemu+ssh://c/system"
S = Settings(heartbeat_suspect_misses=3, heartbeat_dead_after_s=30, heartbeat_ping_timeout_s=0.5)


def host_status(session_factory, hid):
    with session_factory() as s:
        return s.get(Host, hid).status


def test_ha_acceptance(session_factory):
    d = FakeDriver()
    a = add_ready_host(session_factory, d)
    b = add_ready_host(session_factory, d, name="b", uri=URI_B)
    c = add_ready_host(session_factory, d, name="c", uri=URI_C)
    v1 = make_vm(session_factory, d, a, name="v1")
    v2 = make_vm(session_factory, d, a, name="v2")
    # one VM on b so that a is the only host with 2 VMs
    d.define_domain(URI_B, DomainSpec(name="v3", vcpus=1, mem_mb=512, disk_path="/d/v3.qcow2"))
    d.create_disk_overlay("/base.qcow2", "/d/v3.qcow2")
    d.start_domain(URI_B, "v3")
    with session_factory() as s, s.begin():
        s.add(
            Vm(
                name="v3",
                host_id=b,
                vcpus=1,
                mem_mb=512,
                image="x",
                disk_path="/d/v3.qcow2",
                desired_state="running",
                actual_state="running",
            )
        )

    t0 = datetime.now(UTC) - timedelta(seconds=60)
    heartbeat_once(session_factory, d, S, now=t0)
    d.power_off_host(URI)
    for secs in (5, 10, 15):
        heartbeat_once(session_factory, d, S, now=t0 + timedelta(seconds=secs))
    assert host_status(session_factory, a) == "suspect"
    failover_once(session_factory, S)
    assert drain(session_factory, d) == []  # suspect is never fenced
    heartbeat_once(session_factory, d, S, now=t0 + timedelta(seconds=31))
    assert host_status(session_factory, a) == "dead"

    failover_once(session_factory, S)
    assert len(drain(session_factory, d)) == 2

    with session_factory() as s:
        for vid, name in ((v1, "v1"), (v2, "v2")):
            vm = s.get(Vm, vid)
            assert vm.host_id in (b, c) and vm.actual_state == "running"
            uri = s.get(Host, vm.host_id).libvirt_uri
            assert d.domain_state(uri, name) == DomainState.RUNNING
        rows = s.scalars(select(HaRestart)).all()
        assert sorted(r.vm_id for r in rows) == [v1, v2]
        assert all(r.recovery_ms is not None and r.recovery_ms >= 30_000 for r in rows)

    d.power_on_host(URI)
    heartbeat_once(session_factory, d, S)
    assert host_status(session_factory, a) == "dead"  # stays dead until reclaimed
    drain(session_factory, d)
    assert host_status(session_factory, a) == "ready"
    assert d.list_domains(URI) == {}  # stale copies gone
    assert_db_matches_driver(session_factory, d)
