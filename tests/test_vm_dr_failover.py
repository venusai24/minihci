import logging
from datetime import UTC, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from minihci.app import create_app
from minihci.config import Settings
from minihci.dr.scheduler import replicate_once
from minihci.driver.base import DomainState
from minihci.driver.fake import FakeDriver
from minihci.ha.failover import failover_once
from minihci.models import DrFailover, Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.engine import enqueue
from minihci.tasks.executor import StepContext
from tests.helpers import add_ready_host, drain, load_task
from tests.test_vm_migrate import URI_B
from tests.test_vm_power import URI, make_vm, run

URI_DR = "qemu:///system"


class RecordingDriver(FakeDriver):
    """FakeDriver that records the order of destroy/start calls (to pin "destroy the primary before the DR start")."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []

    def destroy_domain(self, uri, name):
        self.calls.append(("destroy_domain", uri, name))
        super().destroy_domain(uri, name)

    def start_domain(self, uri, name):
        self.calls.append(("start_domain", uri, name))
        super().start_domain(uri, name)


@pytest.fixture
def driver():
    return RecordingDriver()


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(Settings(), session_factory=session_factory))


@pytest.fixture
def host(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def dr_host(session_factory, driver):
    return add_dr_host(session_factory, driver)


def add_dr_host(session_factory, driver, name="dr", uri=URI_DR, status="ready") -> int:
    hid = add_ready_host(session_factory, driver, name=name, uri=uri)
    with session_factory() as s, s.begin():
        h = s.get(Host, hid)
        h.site = "dr"
        h.status = status
    return hid


@pytest.fixture
def vm(session_factory, driver, host):
    vid = make_vm(session_factory, driver, host)
    with session_factory() as s, s.begin():
        s.add(ProtectionPolicy(vm_id=vid, interval_s=60, retention=5, enabled=True))
    return vid


def replicate(session_factory, driver, vid, blocks) -> Replica:
    with session_factory() as s:
        name = s.get(Vm, vid).name
    driver.write_blocks(URI, name, blocks)
    assert run(session_factory, driver, "replicate", vid).state == "succeeded"
    with session_factory() as s:
        return s.scalars(select(Replica).where(Replica.vm_id == vid).order_by(Replica.id.desc())).first()


@pytest.fixture
def replicas(session_factory, driver, vm):
    """Two succeeded replicas (a full and an incremental); the newest is last."""
    return [replicate(session_factory, driver, vm, [1, 2, 3]), replicate(session_factory, driver, vm, [2, 7])]


def set_host(session_factory, hid, status, last_heartbeat=None) -> None:
    with session_factory() as s, s.begin():
        h = s.get(Host, hid)
        h.status = status
        h.last_heartbeat = last_heartbeat


def kill_primary(session_factory, driver, hid, replica: Replica, after_s=42) -> None:
    """Power the primary off and mark it dead, last good heartbeat `after_s` after the replica's snapshot."""
    driver.power_off_host(URI)
    hb = replica.snapshot_time.astimezone(UTC).replace(tzinfo=None) + timedelta(seconds=after_s)
    set_host(session_factory, hid, "dead", hb)  # hosts.last_heartbeat is naive UTC


def failover(client, vid, key=None, **body):
    headers = {"Idempotency-Key": key} if key else {}
    if body:
        return client.post(f"/vms/{vid}/failover", json=body, headers=headers)
    return client.post(f"/vms/{vid}/failover", headers=headers)  # absent body = defaults


def fo_row(session_factory, task_id) -> DrFailover:
    with session_factory() as s:
        return s.scalars(select(DrFailover).where(DrFailover.task_id == task_id)).one()


def vm_row(session_factory, vid) -> Vm:
    with session_factory() as s:
        return s.get(Vm, vid)


def policy_enabled(session_factory, vid) -> bool:
    with session_factory() as s:
        return s.get(ProtectionPolicy, vid).enabled


def disks(driver) -> set[str]:
    with driver._locked() as state:
        return set(state["disks"])


# ---- the happy path -------------------------------------------------------------------------------------------------


def test_failover_boots_replica_on_dr_host(client, session_factory, driver, host, dr_host, vm, replicas, caplog):
    latest = replicas[-1]
    kill_primary(session_factory, driver, host, latest, after_s=42)
    old_disk = vm_row(session_factory, vm).disk_path
    r = failover(client, vm)
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    assert r.json()["target_id"] == vm
    assert r.headers["location"] == f"/tasks/{tid}"
    with session_factory() as s:
        t = s.get(Task, tid)
        assert t.type == "vm.dr_failover"
        assert t.params == {"replica_id": latest.id, "dr_host_id": dr_host, "force": False}
    with caplog.at_level(logging.INFO, logger="minihci.tasks"):
        drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"

    row = fo_row(session_factory, tid)
    overlay = f"/srv/minihci/dr/vm{vm}-failover-t{tid}.qcow2"
    assert (row.vm_id, row.replica_id, row.from_host_id, row.dr_host_id) == (vm, latest.id, host, dr_host)
    assert (row.from_disk_path, row.disk_path, row.forced) == (old_disk, overlay, False)
    assert row.running_at is not None and row.failover_ms is not None and row.failover_ms >= 0
    assert row.primary_failed_at == latest.snapshot_time + timedelta(seconds=42)
    assert row.data_loss_s == pytest.approx(42.0)

    assert driver.domain_state(URI_DR, "web1") == DomainState.RUNNING
    assert driver.read_disk(overlay) == driver.read_disk(latest.dr_path) != {}
    v = vm_row(session_factory, vm)
    assert (v.host_id, v.disk_path, v.desired_state, v.actual_state, v.state_reason) == (
        dr_host,
        overlay,
        "running",
        "running",
        None,
    )
    assert policy_enabled(session_factory, vm) is False
    events = [getattr(rec, "fields", {}).get("event") for rec in caplog.records]
    assert "dr_failover_running" in events


def test_failover_picks_requested_replica_and_host(client, session_factory, driver, host, dr_host, vm, replicas):
    second_dr = add_dr_host(session_factory, driver, name="dr2", uri="qemu+ssh://dr2/system")
    kill_primary(session_factory, driver, host, replicas[0])
    r = failover(client, vm, replica_id=replicas[0].id, host_id=second_dr)
    assert r.status_code == 202, r.text
    drain(session_factory, driver)
    row = fo_row(session_factory, r.json()["task_id"])
    assert (row.replica_id, row.dr_host_id) == (replicas[0].id, second_dr)
    assert driver.read_disk(row.disk_path) == driver.read_disk(replicas[0].dr_path)
    assert driver.domain_state("qemu+ssh://dr2/system", "web1") == DomainState.RUNNING
    assert driver.domain_state(URI_DR, "web1") is None


# ---- Review Focus 5: fencing ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["ready", "suspect"])
def test_failover_requires_dead_unless_force(client, session_factory, driver, host, dr_host, vm, replicas, status):
    set_host(session_factory, host, status)
    r = failover(client, vm)
    assert r.status_code == 409
    assert "not dead" in r.json()["detail"]
    r = failover(client, vm, force=False)
    assert r.status_code == 409
    with session_factory() as s:
        assert s.scalars(select(Task).where(Task.type == "vm.dr_failover")).all() == []

    r = failover(client, vm, force=True)
    assert r.status_code == 202, r.text
    tid = r.json()["task_id"]
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    row = fo_row(session_factory, tid)
    assert row.forced is True
    assert row.primary_failed_at is None and row.data_loss_s is None  # the primary was not known to have failed
    assert vm_row(session_factory, vm).host_id == dr_host


def test_fence_refuses_when_host_returns(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    before = vm_row(session_factory, vm)
    r = failover(client, vm)
    assert r.status_code == 202
    tid = r.json()["task_id"]
    driver.power_on_host(URI)
    set_host(session_factory, host, "ready")  # the host came back between the API check and the worker
    drain(session_factory, driver)
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back"
    assert "not dead" in t.error
    overlay = f"/srv/minihci/dr/vm{vm}-failover-t{tid}.qcow2"
    assert driver.domain_state(URI_DR, "web1") is None
    assert not driver.disk_exists(overlay)
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING  # the primary copy is untouched
    after = vm_row(session_factory, vm)
    assert (after.host_id, after.disk_path, after.actual_state) == (before.host_id, before.disk_path, "running")
    assert policy_enabled(session_factory, vm) is True
    assert fo_row(session_factory, tid).running_at is None


def test_fence_refuses_when_dead_host_still_runs_domain(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    r = failover(client, vm)
    tid = r.json()["task_id"]
    driver.power_on_host(URI)  # still `dead` in the DB (partition), but it answers with the domain running
    drain(session_factory, driver)
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back"
    assert "refusing to start a second copy" in t.error
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING  # not forced: never destroyed
    assert driver.domain_state(URI_DR, "web1") is None
    assert not driver.disk_exists(fo_row(session_factory, tid).disk_path)


def test_fence_refuses_while_host_reclaim_open(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    r = failover(client, vm)
    tid = r.json()["task_id"]
    with session_factory() as s, s.begin():
        s.add(Task(type="host.reclaim", target_id=host, state="running"))  # the host answered a ping again
    drain(session_factory, driver)
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back"
    assert "host.reclaim" in t.error
    assert driver.domain_state(URI_DR, "web1") is None


def test_force_destroys_reachable_primary(client, session_factory, driver, host, dr_host, vm, replicas):
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING  # primary is `ready` and runs the VM
    r = failover(client, vm, force=True)
    assert r.status_code == 202
    tid = r.json()["task_id"]
    driver.calls.clear()
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    assert driver.domain_state(URI, "web1") == DomainState.SHUT_OFF
    assert driver.domain_state(URI_DR, "web1") == DomainState.RUNNING
    assert driver.calls.index(("destroy_domain", URI, "web1")) < driver.calls.index(("start_domain", URI_DR, "web1"))
    assert fo_row(session_factory, tid).data_loss_s is None


def test_force_treats_unreachable_primary_as_fenced(client, session_factory, driver, host, dr_host, vm, replicas):
    driver.power_off_host(URI)
    set_host(session_factory, host, "suspect")
    r = failover(client, vm, force=True)
    tid = r.json()["task_id"]
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    assert ("destroy_domain", URI, "web1") not in driver.calls
    assert driver.domain_state(URI_DR, "web1") == DomainState.RUNNING


def test_start_refences_after_resume(session_factory, driver, host, dr_host, vm, replicas):
    """A task resumed after fence_primary was done must not start the DR copy if the host came back meanwhile."""
    from minihci.tasks.types import vm_dr_failover as fo

    kill_primary(session_factory, driver, host, replicas[-1])
    ctx = step_ctx(session_factory, driver, vm, replicas[-1].id, dr_host)
    for step in (fo.record, fo.fence_primary, fo.prepare_disk, fo.define_domain):
        step(ctx)
    set_host(session_factory, host, "ready")
    with pytest.raises(RuntimeError, match="not dead"):
        fo.start_domain(ctx)
    assert driver.domain_state(URI_DR, "web1") == DomainState.SHUT_OFF


# ---- API refusals ---------------------------------------------------------------------------------------------------


def test_missing_vm_404(client, session_factory, dr_host):
    assert failover(client, 999).status_code == 404


def test_replica_of_other_vm_409(client, session_factory, driver, host, dr_host, vm, replicas):
    other = make_vm(session_factory, driver, host, name="web2")
    with session_factory() as s, s.begin():
        s.add(ProtectionPolicy(vm_id=other, interval_s=60, retention=5, enabled=True))
    theirs = replicate(session_factory, driver, other, [4])
    kill_primary(session_factory, driver, host, replicas[-1])
    r = failover(client, vm, replica_id=theirs.id)
    assert r.status_code == 409
    assert "replica" in r.json()["detail"]
    assert failover(client, vm, replica_id=987654).status_code == 409
    with session_factory() as s, s.begin():
        s.get(Replica, replicas[0].id).status = "pruned"
    assert failover(client, vm, replica_id=replicas[0].id).status_code == 409


def test_no_succeeded_replica_409(client, session_factory, driver, host, dr_host, vm):
    set_host(session_factory, host, "dead")
    r = failover(client, vm)
    assert r.status_code == 409
    assert "replica" in r.json()["detail"]


def test_no_dr_host_409(client, session_factory, driver, host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    r = failover(client, vm)
    assert r.status_code == 409
    assert "DR" in r.json()["detail"]
    other_primary = add_ready_host(session_factory, driver, name="b", uri=URI_B)
    assert failover(client, vm, host_id=other_primary).status_code == 409
    down = add_dr_host(session_factory, driver, status="suspect")
    assert failover(client, vm).status_code == 409
    assert failover(client, vm, host_id=down).status_code == 409
    with session_factory() as s:
        assert s.scalars(select(Task).where(Task.type == "vm.dr_failover")).all() == []


def test_dr_host_without_capacity_409(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    with session_factory() as s, s.begin():
        s.get(Host, dr_host).mem_total_mb = 256  # the VM needs 512
    r = failover(client, vm)
    assert r.status_code == 409
    assert "capacity" in r.json()["detail"]


def test_open_task_409(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    with session_factory() as s, s.begin():
        enqueue(s, "vm.stop", vm, {})
    r = failover(client, vm)
    assert r.status_code == 409
    assert "open" in r.json()["detail"]


def test_already_on_dr_site_409(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    assert failover(client, vm).status_code == 202
    drain(session_factory, driver)
    for body in ({}, {"force": True}):
        r = failover(client, vm, **body)
        assert r.status_code == 409
        assert "DR site" in r.json()["detail"]


def test_replay_returns_original_task(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    first = failover(client, vm, key="fo-1")
    assert first.status_code == 202
    tid = first.json()["task_id"]
    with session_factory() as s, s.begin():  # a newer replica appears: "latest" would now resolve differently
        r = s.get(Replica, replicas[-1].id)
        t = Task(type="vm.replicate", target_id=vm, state="succeeded")
        s.add(t)
        s.flush()
        newer = Replica(
            vm_id=vm,
            task_id=t.id,
            host_id=r.host_id,
            mode="incremental",
            status="succeeded",
            seq=r.seq + 1,
            chain_id=r.chain_id,
            dr_path="/srv/minihci/dr/newer.qcow2",
            snapshot_time=r.snapshot_time + timedelta(seconds=5),
        )
        s.add(newer)
    again = failover(client, vm, key="fo-1")
    assert again.status_code == 202
    assert again.json()["task_id"] == tid
    assert again.headers["idempotent-replayed"] == "true"
    assert load_task(session_factory, tid).params["replica_id"] == replicas[-1].id


# ---- task behaviour -------------------------------------------------------------------------------------------------


def step_ctx(session_factory, driver, vid, replica_id, dr_host_id, force=False) -> StepContext:
    params = {"replica_id": replica_id, "dr_host_id": dr_host_id, "force": force}
    with session_factory() as s, s.begin():
        tid = enqueue(s, "vm.dr_failover", vid, params).id
    return StepContext(
        task_id=tid,
        target_id=vid,
        params=params,
        driver=driver,
        session_factory=session_factory,
        settings=Settings(),
        log=logging.getLogger("minihci.tasks"),
    )


def _snapshot(session_factory, driver, ctx):
    with session_factory() as s:
        row = s.scalars(select(DrFailover).where(DrFailover.task_id == ctx.task_id)).one_or_none()
        v = s.get(Vm, ctx.target_id)
        p = s.get(ProtectionPolicy, ctx.target_id)
        rowt = (
            None
            if row is None
            else (row.id, row.disk_path, row.started_at, row.running_at, row.failover_ms, row.data_loss_s)
        )
        vmt = (v.host_id, v.disk_path, v.desired_state, v.actual_state)
        enabled = p.enabled
    return (
        rowt,
        vmt,
        enabled,
        driver.domain_state(URI_DR, "web1"),
        sorted(disks(driver)),
        {d: driver.read_disk(d) for d in sorted(disks(driver))},
    )


def test_each_step_safe_to_rerun(session_factory, driver, host, dr_host, vm, replicas):
    from minihci.tasks.types import vm_dr_failover as fo

    kill_primary(session_factory, driver, host, replicas[-1])
    ctx = step_ctx(session_factory, driver, vm, replicas[-1].id, dr_host)
    for step in (fo.record, fo.fence_primary, fo.prepare_disk, fo.define_domain, fo.start_domain, fo.switch):
        step(ctx)
        before = _snapshot(session_factory, driver, ctx)
        step(ctx)
        assert _snapshot(session_factory, driver, ctx) == before, step.__name__
    assert driver.domain_state(URI_DR, "web1") == DomainState.RUNNING
    assert vm_row(session_factory, vm).host_id == dr_host


def test_missing_replica_file_refuses_without_side_effects(
    client, session_factory, driver, host, dr_host, vm, replicas
):
    kill_primary(session_factory, driver, host, replicas[-1])
    r = failover(client, vm)
    tid = r.json()["task_id"]
    driver.delete_disk(replicas[-1].dr_path)  # someone removed the file from the DR share
    before = disks(driver)
    drain(session_factory, driver)
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back"
    assert "replica" in t.error
    assert disks(driver) == before
    assert driver.domain_state(URI_DR, "web1") is None
    assert vm_row(session_factory, vm).host_id == host


def test_undo_after_switch_leaves_dr_copy_running(session_factory, driver, host, dr_host, vm, replicas):
    """If a rollback ever reaches a task whose switch committed, the undos must not destroy the live DR copy."""
    from minihci.tasks.types import vm_dr_failover as fo

    kill_primary(session_factory, driver, host, replicas[-1])
    ctx = step_ctx(session_factory, driver, vm, replicas[-1].id, dr_host)
    for step in (fo.record, fo.fence_primary, fo.prepare_disk, fo.define_domain, fo.start_domain, fo.switch):
        step(ctx)
    for undo in (fo.undo_start_domain, fo.undo_define_domain, fo.undo_prepare_disk):
        with pytest.raises(RuntimeError, match="switched"):
            undo(ctx)
    assert driver.domain_state(URI_DR, "web1") == DomainState.RUNNING
    assert driver.disk_exists(fo_row(session_factory, ctx.task_id).disk_path)


def test_replicas_untouched_after_failover(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    before = {r.dr_path: driver.read_disk(r.dr_path) for r in replicas}
    r = failover(client, vm)
    drain(session_factory, driver)
    overlay = fo_row(session_factory, r.json()["task_id"]).disk_path
    driver.write_blocks(URI_DR, "web1", [1, 99])  # the guest writes on the DR copy
    for rep in replicas:
        assert driver.disk_exists(rep.dr_path)
    assert {r.dr_path: driver.read_disk(r.dr_path) for r in replicas} == before
    assert driver.read_disk(overlay) != driver.read_disk(replicas[-1].dr_path)


def test_scheduler_ignores_failed_over_vm(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    failover(client, vm)
    drain(session_factory, driver)
    assert vm_row(session_factory, vm).host_id == dr_host
    set_host(session_factory, host, "ready")  # even with the old primary back, the VM now lives at the DR site
    assert replicate_once(session_factory, Settings(), now=replicas[-1].snapshot_time + timedelta(days=1)) == []


def test_ha_ignores_failed_over_vm(client, session_factory, driver, host, dr_host, vm, replicas):
    kill_primary(session_factory, driver, host, replicas[-1])
    failover(client, vm)
    drain(session_factory, driver)
    failover_once(session_factory, Settings())
    with session_factory() as s:
        assert s.scalars(select(Task).where(Task.type == "vm.ha_restart")).all() == []
        assert s.get(Vm, vm).actual_state == "running"


def test_rolled_back_logs_event(client, session_factory, driver, host, dr_host, vm, replicas, caplog):
    kill_primary(session_factory, driver, host, replicas[-1])
    r = failover(client, vm)
    set_host(session_factory, host, "ready")
    with caplog.at_level(logging.INFO, logger="minihci.tasks"):
        drain(session_factory, driver)
    assert load_task(session_factory, r.json()["task_id"]).state == "rolled_back"
    recs = [rec for rec in caplog.records if getattr(rec, "fields", {}).get("event") == "dr_failover_rolled_back"]
    assert recs and recs[0].fields["vm_id"] == vm and recs[0].fields["replica_id"] == replicas[-1].id
