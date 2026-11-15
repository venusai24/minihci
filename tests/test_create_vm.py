import pytest
from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.base import DomainState
from minihci.driver.fake import FakeDriver
from minihci.models import Host, Task
from tests.helpers import add_ready_host, assert_db_matches_driver, drain, load_task

URI = "qemu+ssh://a/system"
STEPS = ["create_disk", "define_domain", "start_domain", "mark_running"]


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(Settings(storage_root="/srv/test-images"), session_factory=session_factory))


def body(host_id, **kw):
    return {"name": "web1", "host_id": host_id, "vcpus": 1, "mem_mb": 512, "image": "/img/alpine.qcow2"} | kw


def test_create_vm_end_to_end(client, session_factory, driver, host_id):
    r = client.post("/vms", json=body(host_id))
    assert r.status_code == 202
    vm = client.get("/vms").json()[0]
    assert (vm["desired_state"], vm["actual_state"]) == ("running", "pending")
    assert vm["disk_path"] == "/srv/test-images/web1.qcow2"
    drain(session_factory, driver)
    t = client.get(f"/tasks/{r.json()['task_id']}").json()
    assert t["state"] == "succeeded" and [s["name"] for s in t["steps"]] == STEPS
    assert driver.domain_state(URI, "web1") == DomainState.RUNNING
    assert client.get("/vms").json()[0]["actual_state"] == "running"
    assert_db_matches_driver(session_factory, driver)


@pytest.mark.parametrize("idx", range(len(STEPS)))
def test_each_step_safe_to_rerun(client, session_factory, driver, host_id, idx):
    """Simulate a crash right after step `idx` acted but before it was recorded done."""
    tid = client.post("/vms", json=body(host_id)).json()["task_id"]
    drain(session_factory, driver)
    with session_factory() as s:
        t = s.get(Task, tid)
        t.steps = [{**st, "status": "done" if i < idx else ("running" if i == idx else "pending")}
                   for i, st in enumerate(t.steps)]
        t.state = "pending"
        s.commit()
    drain(session_factory, driver)
    assert load_task(session_factory, tid).state == "succeeded"
    assert_db_matches_driver(session_factory, driver)


def test_start_failure_rolls_back_everything(client, session_factory, driver, host_id):
    driver.fail["start_domain"] = 1
    tid = client.post("/vms", json=body(host_id)).json()["task_id"]
    drain(session_factory, driver)
    t = load_task(session_factory, tid)
    assert t.state == "rolled_back" and "injected failure: start_domain" in t.error
    assert driver.domain_state(URI, "web1") is None
    assert not driver.disk_exists("/srv/test-images/web1.qcow2")
    vm = client.get("/vms").json()[0]
    assert (vm["desired_state"], vm["actual_state"]) == ("absent", "absent")
    assert_db_matches_driver(session_factory, driver)


@pytest.mark.parametrize("override", [
    {"name": "../etc"}, {"name": "Web 1"}, {"name": ""}, {"name": "a" * 64},
    {"vcpus": 0}, {"mem_mb": -1},
])
def test_vm_validation(client, host_id, override):
    assert client.post("/vms", json=body(host_id, **override)).status_code == 422


def test_unknown_host_404(client, host_id):
    assert client.post("/vms", json=body(999)).status_code == 404


def test_host_not_ready_409(client, session_factory, host_id):
    with session_factory() as s:
        s.get(Host, host_id).status = "registering"
        s.commit()
    assert client.post("/vms", json=body(host_id)).status_code == 409


def test_duplicate_name_409(client, host_id):
    assert client.post("/vms", json=body(host_id)).status_code == 202
    assert client.post("/vms", json=body(host_id)).status_code == 409


def test_replay_after_success_returns_original(client, session_factory, driver, host_id):
    h = {"Idempotency-Key": "create-web1"}
    a = client.post("/vms", json=body(host_id), headers=h)
    drain(session_factory, driver)
    b = client.post("/vms", json=body(host_id), headers=h)
    assert b.status_code == 202 and b.json() == a.json()
    assert len(client.get("/vms").json()) == 1
