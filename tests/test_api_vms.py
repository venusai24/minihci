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


# --- auto placement (Q06.3, FR-3.2, D-022) ---


def _add_host(session_factory, name, cpu, mem, status="ready"):
    from minihci.models import Host

    with session_factory() as s:
        h = Host(name=name, libvirt_uri=f"qemu+ssh://{name}/system", status=status, cpu_total=cpu, mem_total_mb=mem)
        s.add(h)
        s.commit()
        return h.id


def _auto_body(**kw):
    return {"name": "auto1", "vcpus": 1, "mem_mb": 512, "image": "/srv/minihci/base/alpine.qcow2"} | kw


def _counts(session_factory):
    from sqlalchemy import func, select

    from minihci.models import Task

    with session_factory() as s:
        return s.scalar(select(func.count()).select_from(Vm)), s.scalar(select(func.count()).select_from(Task))


def test_auto_place_best_fit(client, session_factory):
    _add_host(session_factory, "big", 8, 16384)
    small = _add_host(session_factory, "small", 8, 1024)
    r = client.post("/vms", json=_auto_body())
    assert r.status_code == 202
    assert client.get("/vms").json()[0]["host_id"] == small
    # second VM no longer fits on small (512 left, needs 600): goes to big
    r = client.post("/vms", json=_auto_body(name="auto2", mem_mb=600))
    assert r.status_code == 202
    assert client.get("/vms").json()[1]["host_id"] != small


def test_auto_place_no_capacity_409_no_rows(client, session_factory):
    _add_host(session_factory, "tiny", 8, 256)
    _add_host(session_factory, "down", 8, 16384, status="failed")
    r = client.post("/vms", json=_auto_body())
    assert r.status_code == 409
    assert _counts(session_factory) == (0, 0)


def test_explicit_host_id_unchanged(client, session_factory):
    tiny = _add_host(session_factory, "tiny", 8, 256)  # would not fit; explicit skips capacity check
    r = client.post("/vms", json=_auto_body(host_id=tiny))
    assert r.status_code == 202
    assert client.get("/vms").json()[0]["host_id"] == tiny


def test_auto_place_replay_same_task(client, session_factory):
    _add_host(session_factory, "a", 8, 16384)
    h = {"Idempotency-Key": "auto-k"}
    a = client.post("/vms", json=_auto_body(), headers=h)
    b = client.post("/vms", json=_auto_body(), headers=h)
    assert a.status_code == b.status_code == 202
    assert b.json()["task_id"] == a.json()["task_id"]
    assert b.headers["Idempotent-Replayed"] == "true"
    assert _counts(session_factory) == (1, 1)


# --- migrate (Q08.4, FR-3.1, D-024) ---

URI_B = "qemu+ssh://b/system"


@pytest.fixture
def dst_id(session_factory, driver):
    return add_ready_host(session_factory, driver, name="b", uri=URI_B)


def _migrate(client, vm_id, host, **h):
    return client.post(f"/vms/{vm_id}/migrate", json={"host_id": host}, headers=h)


def test_migrate_202_then_host_changes(client, session_factory, driver, host_id, dst_id):
    vm_id = make_vm(session_factory, driver, host_id)
    r = _migrate(client, vm_id, dst_id)
    assert r.status_code == 202
    assert r.headers["Location"] == f"/tasks/{r.json()['task_id']}"
    assert r.json()["target_id"] == vm_id
    drain(session_factory, driver)
    assert task_state(client, r) == "succeeded"
    assert client.get(f"/vms/{vm_id}").json()["host_id"] == dst_id
    assert_db_matches_driver(session_factory, driver)


def _no_new_task(session_factory, before):
    assert _counts(session_factory)[1] == before


def test_migrate_unknown_vm_404(client, dst_id):
    assert _migrate(client, 999, dst_id).status_code == 404


def test_migrate_absent_vm_404(client, session_factory, driver, host_id, dst_id):
    vm_id = make_vm(session_factory, driver, host_id)
    with session_factory() as s, s.begin():
        s.get(Vm, vm_id).desired_state = "absent"
    assert _migrate(client, vm_id, dst_id).status_code == 404


def test_migrate_vm_not_running_409(client, session_factory, driver, host_id, dst_id):
    vm_id = make_vm(session_factory, driver, host_id, running=False)
    assert _migrate(client, vm_id, dst_id).status_code == 409
    assert _counts(session_factory)[1] == 0


def test_migrate_same_host_409(client, session_factory, driver, host_id, dst_id):
    vm_id = make_vm(session_factory, driver, host_id)
    assert _migrate(client, vm_id, host_id).status_code == 409
    assert _counts(session_factory)[1] == 0


def test_migrate_dst_not_placeable_409(client, session_factory, driver, host_id):
    vm_id = make_vm(session_factory, driver, host_id)
    bad = _add_host(session_factory, "down", 8, 16384, status="failed")
    assert _migrate(client, vm_id, bad).status_code == 409
    assert _counts(session_factory)[1] == 0


def test_migrate_unknown_dst_409(client, session_factory, driver, host_id):
    vm_id = make_vm(session_factory, driver, host_id)
    assert _migrate(client, vm_id, 9999).status_code == 409
    assert _counts(session_factory)[1] == 0


def test_migrate_no_capacity_409(client, session_factory, driver, host_id):
    vm_id = make_vm(session_factory, driver, host_id)
    tiny = _add_host(session_factory, "tiny", 8, 256)
    assert _migrate(client, vm_id, tiny).status_code == 409
    assert _counts(session_factory)[1] == 0


def test_migrate_open_task_409(client, session_factory, driver, host_id, dst_id):
    vm_id = make_vm(session_factory, driver, host_id)
    assert _migrate(client, vm_id, dst_id).status_code == 202
    assert _migrate(client, vm_id, dst_id).status_code == 409
    assert _counts(session_factory)[1] == 1
    drain(session_factory, driver)
    d = client.delete(f"/vms/{vm_id}")
    assert d.status_code == 202
    assert _migrate(client, vm_id, host_id).status_code == 409
    assert _counts(session_factory)[1] == 2


def test_migrate_replay_same_task(client, session_factory, driver, host_id, dst_id):
    vm_id = make_vm(session_factory, driver, host_id)
    h = {"Idempotency-Key": "mig-k"}
    a = _migrate(client, vm_id, dst_id, **h)
    b = _migrate(client, vm_id, dst_id, **h)
    assert a.status_code == b.status_code == 202
    assert b.json()["task_id"] == a.json()["task_id"]
    assert b.headers["Idempotent-Replayed"] == "true"
    assert _counts(session_factory)[1] == 1


def test_list_vm_migrations(client, session_factory, driver, host_id):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func, select

    from minihci.models import Host, Migration, Task

    vm = make_vm(session_factory, driver, host_id)
    with session_factory() as s, s.begin():
        h2 = Host(name="b", libvirt_uri="qemu+ssh://b/system", status="ready", cpu_total=8, mem_total_mb=16384)
        s.add(h2)
        t1 = Task(type="vm.migrate", target_id=vm, state="succeeded")
        t2 = Task(type="vm.migrate", target_id=vm, state="failed")
        s.add_all([t1, t2])
        s.flush()
        h2_id, t1_id, t2_id = h2.id, t1.id, t2.id
        now = datetime.now(UTC)
        s.add(
            Migration(
                task_id=t1_id,
                vm_id=vm,
                src_host_id=host_id,
                dst_host_id=h2_id,
                status="succeeded",
                started_at=now - timedelta(minutes=5),
                finished_at=now - timedelta(minutes=4),
                duration_ms=1200,
                downtime_ms=80,
            )
        )
        s.add(
            Migration(
                task_id=t2_id,
                vm_id=vm,
                src_host_id=h2_id,
                dst_host_id=host_id,
                status="failed",
                started_at=now,
                finished_at=now,
            )
        )
        tasks_before = s.scalar(select(func.count()).select_from(Task))

    r = client.get(f"/vms/{vm}/migrations")
    assert r.status_code == 200
    body = r.json()
    assert [m["task_id"] for m in body] == [t2_id, t1_id]
    assert body[1] == {
        "task_id": t1_id,
        "src_host_id": host_id,
        "dst_host_id": h2_id,
        "status": "succeeded",
        "duration_ms": 1200,
        "downtime_ms": 80,
        "started_at": body[1]["started_at"],
        "finished_at": body[1]["finished_at"],
    }
    assert body[0]["duration_ms"] is None and body[0]["status"] == "failed"
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Task)) == tasks_before


def test_list_vm_migrations_unknown_vm_404(client):
    assert client.get("/vms/9999/migrations").status_code == 404


def test_list_vm_migrations_empty(client, session_factory, driver, host_id):
    vm = make_vm(session_factory, driver, host_id)
    r = client.get(f"/vms/{vm}/migrations")
    assert r.status_code == 200 and r.json() == []
