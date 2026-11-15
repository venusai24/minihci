import pytest
from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Vm
from tests.helpers import add_ready_host, assert_db_matches_driver, drain
from tests.test_vm_power import URI, actual, make_vm


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def host_id(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(Settings(), session_factory=session_factory))


def task_state(client, r):
    return client.get(f"/tasks/{r.json()['task_id']}").json()["state"]


def test_each_operation(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id, running=False)
    r = client.post(f"/vms/{vm}/start")
    assert r.status_code == 202 and r.headers["Location"] == f"/tasks/{r.json()['task_id']}"
    assert r.json()["target_id"] == vm
    assert client.get(f"/vms/{vm}").json()["desired_state"] == "running"
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded" and actual(session_factory, vm) == "running"

    r = client.post(f"/vms/{vm}/reboot")
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded" and driver.boot_count(URI, "web1") == 2

    r = client.post(f"/vms/{vm}/stop", json={"force": True})
    assert client.get(f"/vms/{vm}").json()["desired_state"] == "stopped"
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded" and actual(session_factory, vm) == "stopped"

    r = client.post(f"/vms/{vm}/stop")  # no body: graceful
    assert r.status_code == 202
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded"

    r = client.delete(f"/vms/{vm}")
    assert r.status_code == 202
    assert client.get(f"/vms/{vm}").json()["desired_state"] == "stopped"  # D-018: not absent yet
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded" and actual(session_factory, vm) == "absent"
    assert_db_matches_driver(session_factory, driver)


def test_start_running_is_noop_success(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    driver.fail = {"start_domain": 1}
    r = client.post(f"/vms/{vm}/start")
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded"


def test_replay_and_key_reuse(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    h = {"Idempotency-Key": "k1"}
    a = client.post(f"/vms/{vm}/reboot", headers=h)
    b = client.post(f"/vms/{vm}/reboot", headers=h)
    assert b.json()["task_id"] == a.json()["task_id"]
    assert b.headers["Idempotent-Replayed"] == "true"
    assert client.post(f"/vms/{vm}/stop", headers=h).status_code == 409
    h2 = {"Idempotency-Key": "k2"}
    assert client.post(f"/vms/{vm}/stop", json={"force": True}, headers=h2).status_code == 202
    assert client.post(f"/vms/{vm}/stop", json={"force": False}, headers=h2).status_code == 409


def test_unknown_vm_404(client):
    assert client.post("/vms/999/start").status_code == 404
    assert client.post("/vms/999/stop").status_code == 404
    assert client.post("/vms/999/reboot").status_code == 404
    assert client.delete("/vms/999").status_code == 404
    assert client.get("/vms/999").status_code == 404


def test_absent_vm_404(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    with session_factory() as s, s.begin():
        s.get(Vm, vm).desired_state = "absent"
    assert client.post(f"/vms/{vm}/start").status_code == 404


def test_op_after_queued_delete_409(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    assert client.delete(f"/vms/{vm}").status_code == 202
    assert client.post(f"/vms/{vm}/start").status_code == 409
    assert client.delete(f"/vms/{vm}").status_code == 409
    assert client.get(f"/vms/{vm}").json()["desired_state"] == "running"


def test_get_vm_fields(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    body = client.get(f"/vms/{vm}").json()
    assert body == client.get("/vms").json()[0]
    assert body["name"] == "web1" and body["host_id"] == host_id and body["actual_state"] == "running"
