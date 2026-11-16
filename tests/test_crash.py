import json
import signal
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.base import DomainState
from minihci.driver.fake import FakeDriver
from minihci.ha.failover import failover_once
from minihci.ha.heartbeat import heartbeat_once
from minihci.models import HaRestart, Host, Migration, Task, Vm
from tests.helpers import add_ready_host, assert_db_matches_driver, drain, load_task, spawn_worker, wait_for
from tests.test_vm_power import make_vm

TERMINAL = ("succeeded", "failed", "rolled_back")
STEP_TO_METHOD = {
    "create_disk": "create_disk_overlay",
    "define_domain": "define_domain",
    "start_domain": "start_domain",
}
KILL_POINTS = [(step, where) for step in STEP_TO_METHOD for where in ("before", "after")]
DELETE_STEP_TO_METHOD = {
    "destroy_domain": "destroy_domain",
    "undefine_domain": "undefine_domain",
    "delete_disk": "delete_disk",
}
DELETE_KILL_POINTS = [(step, where) for step in DELETE_STEP_TO_METHOD for where in ("before", "after")]


def step_status(session_factory, tid, name):
    steps = load_task(session_factory, tid).steps
    return next((s["status"] for s in steps if s["name"] == name), None)


def start_create_and_kill(session_factory, tmp_path, step, where):
    """POST a VM create, let a slow worker reach `step`, SIGKILL it. Returns (task_id, state_file)."""
    state = tmp_path / "fake.json"
    add_ready_host(session_factory, FakeDriver(state_file=state))
    client = TestClient(create_app(Settings(storage_root="/srv/test-images"), session_factory=session_factory))
    tid = client.post(
        "/vms",
        json={"name": "web1", "host_id": 1, "vcpus": 1, "mem_mb": 512, "image": "/srv/minihci/base/alpine.qcow2"},
    ).json()["task_id"]
    env_key = "MINIHCI_FAKE_DELAYS" if where == "before" else "MINIHCI_FAKE_DELAYS_AFTER"
    w1 = spawn_worker(state, {env_key: json.dumps({STEP_TO_METHOD[step]: 30})})
    try:
        wait_for(lambda: step_status(session_factory, tid, step) == "running", timeout=10)
        time.sleep(0.3)  # inside the injected sleep: effect applied iff where == "after"
        w1.send_signal(signal.SIGKILL)
        w1.wait(timeout=5)
    finally:
        if w1.poll() is None:
            w1.kill()
    assert load_task(session_factory, tid).state == "running"  # orphaned, not silently finished
    return tid, state


@pytest.mark.timeout(60)
@pytest.mark.parametrize("step,where", KILL_POINTS)
def test_kill_mid_create_then_restart_resumes(session_factory, tmp_path, step, where):
    tid, state = start_create_and_kill(session_factory, tmp_path, step, where)
    w2 = spawn_worker(state)
    try:
        wait_for(lambda: load_task(session_factory, tid).state in TERMINAL, timeout=15)
    finally:
        w2.send_signal(signal.SIGTERM)
        w2.wait(timeout=10)
    t = load_task(session_factory, tid)
    assert t.state == "succeeded", t.error
    assert t.attempts == 2
    assert_db_matches_driver(session_factory, FakeDriver(state_file=state))


@pytest.mark.timeout(60)
def test_kill_then_resume_fails_rolls_back_cleanly(session_factory, tmp_path):
    tid, state = start_create_and_kill(session_factory, tmp_path, "define_domain", "after")
    w2 = spawn_worker(state, {"MINIHCI_FAKE_FAIL": json.dumps({"start_domain": 99})})
    try:
        wait_for(lambda: load_task(session_factory, tid).state in TERMINAL, timeout=15)
    finally:
        w2.send_signal(signal.SIGTERM)
        w2.wait(timeout=10)
    assert load_task(session_factory, tid).state == "rolled_back"
    assert_db_matches_driver(session_factory, FakeDriver(state_file=state))


@pytest.mark.timeout(60)
def test_kill_at_max_attempts_rolls_back_on_restart(session_factory, tmp_path):
    tid, state = start_create_and_kill(session_factory, tmp_path, "start_domain", "after")
    w2 = spawn_worker(state, {"MINIHCI_MAX_ATTEMPTS": "1"})
    try:
        wait_for(lambda: load_task(session_factory, tid).state in TERMINAL, timeout=15)
    finally:
        w2.send_signal(signal.SIGTERM)
        w2.wait(timeout=10)
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back" and "max attempts" in t.error
    assert_db_matches_driver(session_factory, FakeDriver(state_file=state))


def start_delete_and_kill(session_factory, tmp_path, step, where):
    """Create a VM (in-process), DELETE it, let a slow worker reach `step`, SIGKILL it."""
    state = tmp_path / "fake.json"
    driver = FakeDriver(state_file=state)
    add_ready_host(session_factory, driver)
    client = TestClient(create_app(Settings(storage_root="/srv/test-images"), session_factory=session_factory))
    r = client.post(
        "/vms",
        json={"name": "web1", "host_id": 1, "vcpus": 1, "mem_mb": 512, "image": "/srv/minihci/base/alpine.qcow2"},
    )
    assert r.status_code == 202
    drain(session_factory, driver)
    assert load_task(session_factory, r.json()["task_id"]).state == "succeeded"
    tid = client.delete("/vms/1").json()["task_id"]
    env_key = "MINIHCI_FAKE_DELAYS" if where == "before" else "MINIHCI_FAKE_DELAYS_AFTER"
    w1 = spawn_worker(state, {env_key: json.dumps({DELETE_STEP_TO_METHOD[step]: 30})})
    try:
        wait_for(lambda: step_status(session_factory, tid, step) == "running", timeout=10)
        time.sleep(0.3)
        w1.send_signal(signal.SIGKILL)
        w1.wait(timeout=5)
    finally:
        if w1.poll() is None:
            w1.kill()
    assert load_task(session_factory, tid).state == "running"
    return tid, state


@pytest.mark.timeout(60)
@pytest.mark.parametrize("step,where", DELETE_KILL_POINTS)
def test_kill_mid_delete_then_restart_resumes(session_factory, tmp_path, step, where):
    tid, state = start_delete_and_kill(session_factory, tmp_path, step, where)
    w2 = spawn_worker(state)
    try:
        wait_for(lambda: load_task(session_factory, tid).state in TERMINAL, timeout=15)
    finally:
        w2.send_signal(signal.SIGTERM)
        w2.wait(timeout=10)
    t = load_task(session_factory, tid)
    assert t.state == "succeeded", t.error
    assert t.attempts == 2
    driver = FakeDriver(state_file=state)
    with session_factory() as s:
        vm = s.get(Vm, 1)
        assert vm.desired_state == "absent" and vm.actual_state == "absent"
        assert driver.domain_state("qemu+ssh://a/system", "web1") is None
        assert not driver.disk_exists(vm.disk_path)
    assert_db_matches_driver(session_factory, driver)


MIGRATE_KILL_POINTS = [("migrate_live", where) for where in ("before", "after")]


def start_migrate_and_kill(session_factory, tmp_path, where):
    """Running VM on host a, POST a migrate to host b, let a slow worker reach migrate_live, SIGKILL it."""
    state = tmp_path / "fake.json"
    driver = FakeDriver(state_file=state)
    src_id = add_ready_host(session_factory, driver)
    dst_id = add_ready_host(session_factory, driver, name="b", uri="qemu+ssh://b/system")
    vm_id = make_vm(session_factory, driver, src_id)
    client = TestClient(create_app(Settings(storage_root="/srv/test-images"), session_factory=session_factory))
    r = client.post(f"/vms/{vm_id}/migrate", json={"host_id": dst_id})
    assert r.status_code == 202
    tid = r.json()["task_id"]
    env_key = "MINIHCI_FAKE_DELAYS" if where == "before" else "MINIHCI_FAKE_DELAYS_AFTER"
    w1 = spawn_worker(state, {env_key: json.dumps({"migrate_live": 30})})
    try:
        wait_for(lambda: step_status(session_factory, tid, "migrate") == "running", timeout=10)
        time.sleep(0.3)  # inside the injected sleep: migration applied iff where == "after"
        w1.send_signal(signal.SIGKILL)
        w1.wait(timeout=5)
    finally:
        if w1.poll() is None:
            w1.kill()
    assert load_task(session_factory, tid).state == "running"
    fresh = FakeDriver(state_file=state)
    with session_factory() as s:
        vm = s.get(Vm, vm_id)
        name = vm.name
        assert vm.host_id == src_id
    on_src = fresh.domain_state("qemu+ssh://a/system", name) is not None
    on_dst = fresh.domain_state("qemu+ssh://b/system", name) is not None
    assert (on_src, on_dst) == ((True, False) if where == "before" else (False, True))
    return tid, state, vm_id, src_id, dst_id


@pytest.mark.timeout(60)
@pytest.mark.parametrize("step,where", MIGRATE_KILL_POINTS)
def test_kill_mid_migrate_then_restart_leaves_vm_on_one_host(session_factory, tmp_path, step, where):
    tid, state, vm_id, _src_id, dst_id = start_migrate_and_kill(session_factory, tmp_path, where)
    w2 = spawn_worker(state)
    try:
        wait_for(lambda: load_task(session_factory, tid).state in TERMINAL, timeout=15)
    finally:
        w2.send_signal(signal.SIGTERM)
        w2.wait(timeout=10)
    t = load_task(session_factory, tid)
    assert t.state == "succeeded", t.error
    assert t.attempts == 2
    driver = FakeDriver(state_file=state)
    with session_factory() as s:
        migs = list(s.scalars(select(Migration).where(Migration.task_id == tid)))
        assert len(migs) == 1
        assert migs[0].status == "succeeded" and migs[0].finished_at is not None
        if where == "after":
            assert migs[0].duration_ms is None
        else:
            assert migs[0].duration_ms is not None and migs[0].duration_ms >= 0
        vm = s.get(Vm, vm_id)
        placed = [h for h in s.scalars(select(Host)) if driver.domain_state(h.libvirt_uri, vm.name) is not None]
        assert [h.id for h in placed] == [vm.host_id] == [dst_id]
        assert vm.actual_state == "running"
    assert_db_matches_driver(session_factory, driver)


HA_URI_A = "qemu+ssh://a/system"
HA_URI_B = "qemu+ssh://b/system"


def start_ha_restart_and_kill(session_factory, tmp_path):
    """Running VM on dead host a, failover pass enqueues vm.ha_restart, a slow worker blocks in start_domain
    on host b (as it would waiting for the NFS lease), SIGKILL it. Returns (task_id, state_file, vm_id, b_id)."""
    state = tmp_path / "fake.json"
    driver = FakeDriver(state_file=state)
    a_id = add_ready_host(session_factory, driver)
    b_id = add_ready_host(session_factory, driver, name="b", uri=HA_URI_B)
    vm_id = make_vm(session_factory, driver, a_id)
    driver.power_off_host(HA_URI_A)
    with session_factory() as s, s.begin():
        h = s.get(Host, a_id)
        h.status, h.last_heartbeat = "dead", datetime.now(UTC) - timedelta(seconds=40)
    failover_once(session_factory, Settings())
    with session_factory() as s:
        (task,) = s.scalars(select(Task).where(Task.type == "vm.ha_restart")).all()
        tid = task.id
    w1 = spawn_worker(state, {"MINIHCI_FAKE_DELAYS": json.dumps({"start_domain": 30})})
    try:
        wait_for(lambda: step_status(session_factory, tid, "start_domain") == "running", timeout=10)
        time.sleep(0.3)
        w1.send_signal(signal.SIGKILL)
        w1.wait(timeout=5)
    finally:
        if w1.poll() is None:
            w1.kill()
    assert load_task(session_factory, tid).state == "running"
    with session_factory() as s:
        assert s.get(Vm, vm_id).host_id == a_id  # mark_running never ran
    return tid, state, vm_id, b_id


@pytest.mark.timeout(60)
def test_kill_mid_ha_restart_then_restart_resumes(session_factory, tmp_path):
    tid, state, vm_id, b_id = start_ha_restart_and_kill(session_factory, tmp_path)
    w2 = spawn_worker(state)
    try:
        wait_for(lambda: load_task(session_factory, tid).state in TERMINAL, timeout=15)
    finally:
        w2.send_signal(signal.SIGTERM)
        w2.wait(timeout=10)
    t = load_task(session_factory, tid)
    assert t.state == "succeeded", t.error
    assert t.attempts == 2
    driver = FakeDriver(state_file=state)
    # host a returns: reclaim removes its stale copy, leaving exactly one running copy
    driver.power_on_host(HA_URI_A)
    heartbeat_once(session_factory, driver, Settings(heartbeat_ping_timeout_s=0.5))
    drain(session_factory, driver)
    assert driver.domain_state(HA_URI_A, "web1") is None
    with session_factory() as s:
        vm = s.get(Vm, vm_id)
        assert vm.host_id == b_id and vm.actual_state == "running"
        running = [
            h.id for h in s.scalars(select(Host)) if driver.domain_state(h.libvirt_uri, vm.name) == DomainState.RUNNING
        ]
        assert running == [b_id]
        (ha,) = s.scalars(select(HaRestart)).all()
        assert ha.running_at is not None and ha.recovery_ms is not None
    assert driver.boot_count(HA_URI_B, "web1") == 1
    assert_db_matches_driver(session_factory, driver)
