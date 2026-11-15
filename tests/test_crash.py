import json
import signal
import time

import pytest
from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Vm
from tests.helpers import add_ready_host, assert_db_matches_driver, drain, load_task, spawn_worker, wait_for

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
