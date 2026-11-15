from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from tests.helpers import drain

URI = "qemu+ssh://a/system"


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(Settings(), session_factory=session_factory))


def test_api_package_never_imports_driver():
    api_dir = Path(__file__).resolve().parents[1] / "src" / "minihci" / "api"
    for f in api_dir.rglob("*.py"):
        assert "minihci.driver" not in f.read_text(), f


def test_register_host_returns_202_and_pending_task(client):
    r = client.post("/hosts", json={"name": "a", "libvirt_uri": URI})
    assert r.status_code == 202
    body = r.json()
    assert set(body) == {"task_id", "target_id"}
    assert r.headers["location"] == f"/tasks/{body['task_id']}"
    t = client.get(f"/tasks/{body['task_id']}").json()
    assert (t["state"], t["type"], t["target_id"]) == ("pending", "host.register", body["target_id"])
    assert client.get("/hosts").json() == [
        {
            "id": body["target_id"],
            "name": "a",
            "libvirt_uri": URI,
            "status": "registering",
            "cpu_total": None,
            "mem_total_mb": None,
        }
    ]


def test_worker_probes_and_marks_ready(client, session_factory):
    drv = FakeDriver()
    drv.add_host(URI, 8, 16384)
    tid = client.post("/hosts", json={"name": "a", "libvirt_uri": URI}).json()["task_id"]
    drain(session_factory, drv)
    t = client.get(f"/tasks/{tid}").json()
    assert t["state"] == "succeeded" and [s["name"] for s in t["steps"]] == ["probe"]
    h = client.get("/hosts").json()[0]
    assert (h["status"], h["cpu_total"], h["mem_total_mb"]) == ("ready", 8, 16384)


def test_unreachable_host_rolls_back_and_is_marked_failed(client, session_factory):
    tid = client.post("/hosts", json={"name": "a", "libvirt_uri": URI}).json()["task_id"]
    drain(session_factory, FakeDriver())  # host unknown to the driver
    t = client.get(f"/tasks/{tid}").json()
    assert t["state"] == "rolled_back"
    assert URI in t["error"]  # FakeDriver raises HostUnreachable(uri)
    assert client.get("/hosts").json()[0]["status"] == "failed"


def test_duplicate_host_conflicts(client):
    client.post("/hosts", json={"name": "a", "libvirt_uri": URI})
    assert client.post("/hosts", json={"name": "a", "libvirt_uri": "qemu+ssh://b/system"}).status_code == 409
    assert client.post("/hosts", json={"name": "b", "libvirt_uri": URI}).status_code == 409


def test_idempotent_register_replays(client):
    h = {"Idempotency-Key": "reg-a"}
    a = client.post("/hosts", json={"name": "a", "libvirt_uri": URI}, headers=h)
    b = client.post("/hosts", json={"name": "a", "libvirt_uri": URI}, headers=h)
    assert b.status_code == 202 and b.json() == a.json()
    assert b.headers["idempotent-replayed"] == "true"
    assert len(client.get("/tasks").json()) == 1


def test_idempotency_key_reused_with_other_body_conflicts(client):
    h = {"Idempotency-Key": "reg-a"}
    client.post("/hosts", json={"name": "a", "libvirt_uri": URI}, headers=h)
    assert client.post("/hosts", json={"name": "z", "libvirt_uri": "qemu+ssh://z/system"}, headers=h).status_code == 409


def test_oversized_idempotency_key_is_400(client):
    r = client.post("/hosts", json={"name": "a", "libvirt_uri": URI}, headers={"Idempotency-Key": "x" * 256})
    assert r.status_code == 400


def test_task_endpoints(client):
    client.post("/hosts", json={"name": "a", "libvirt_uri": URI})
    assert len(client.get("/tasks", params={"state": "pending"}).json()) == 1
    assert client.get("/tasks", params={"state": "succeeded"}).json() == []
    assert client.get("/tasks/999").status_code == 404


def test_stray_value_error_is_a_500_not_a_400(session_factory):
    app = create_app(Settings(), session_factory=session_factory)

    @app.get("/boom")
    def boom():
        raise ValueError("internal bug")

    assert TestClient(app, raise_server_exceptions=False).get("/boom").status_code == 500


# --- DELETE /hosts/{id} (Q02.3) ---


def _ready(session_factory, name="a", uri=URI):
    from tests.helpers import add_ready_host

    drv = FakeDriver()
    return add_ready_host(session_factory, drv, name=name, uri=uri), drv


def test_delete_host_202_then_gone_from_listing(client, session_factory):
    hid, drv = _ready(session_factory)
    r = client.delete(f"/hosts/{hid}")
    assert r.status_code == 202
    assert r.headers["location"] == f"/tasks/{r.json()['task_id']}"
    assert [h["status"] for h in client.get("/hosts").json()] == ["removing"]
    drain(session_factory, drv)
    assert client.get(f"/tasks/{r.json()['task_id']}").json()["state"] == "succeeded"
    assert client.get("/hosts").json() == []


def test_delete_host_with_live_vm_is_409_and_no_task(client, session_factory):
    from minihci.models import Task, Vm

    hid, _ = _ready(session_factory)
    with session_factory() as s:
        s.add(
            Vm(
                name="web1",
                host_id=hid,
                vcpus=1,
                mem_mb=512,
                image="i",
                disk_path="/d",
                desired_state="running",
                actual_state="running",
            )
        )
        s.commit()
    r = client.delete(f"/hosts/{hid}")
    assert r.status_code == 409 and "web1" in r.text
    with session_factory() as s:
        assert s.query(Task).count() == 0
    assert client.get("/hosts").json()[0]["status"] == "ready"


def test_delete_host_replay_same_key(client, session_factory):
    hid, drv = _ready(session_factory)
    h = {"Idempotency-Key": "rm-1"}
    first = client.delete(f"/hosts/{hid}", headers=h)
    second = client.delete(f"/hosts/{hid}", headers=h)
    assert second.status_code == 202
    assert second.json()["task_id"] == first.json()["task_id"]
    assert second.headers["idempotent-replayed"] == "true"
    drain(session_factory, drv)
    third = client.delete(f"/hosts/{hid}", headers=h)  # even after removal
    assert third.json()["task_id"] == first.json()["task_id"]


def test_delete_host_same_key_other_host_conflicts(client, session_factory):
    a, _ = _ready(session_factory)
    b, _ = _ready(session_factory, name="b", uri="qemu+ssh://b/system")
    h = {"Idempotency-Key": "rm-1"}
    assert client.delete(f"/hosts/{a}", headers=h).status_code == 202
    assert client.delete(f"/hosts/{b}", headers=h).status_code == 409


def test_delete_unknown_or_removed_host_is_404(client, session_factory):
    assert client.delete("/hosts/9999").status_code == 404
    hid, drv = _ready(session_factory)
    client.delete(f"/hosts/{hid}")
    drain(session_factory, drv)
    assert client.delete(f"/hosts/{hid}").status_code == 404


def test_delete_host_already_removing_is_409(client, session_factory):
    hid, _ = _ready(session_factory)
    assert client.delete(f"/hosts/{hid}").status_code == 202
    assert client.delete(f"/hosts/{hid}").status_code == 409


def test_create_vm_on_removing_host_is_409(client, session_factory):
    hid, _ = _ready(session_factory)
    client.delete(f"/hosts/{hid}")
    r = client.post(
        "/vms",
        json={"name": "w", "host_id": hid, "vcpus": 1, "mem_mb": 512, "image": "/srv/minihci/base/alpine.qcow2"},
    )
    assert r.status_code == 409


def test_reregister_same_name_and_uri_after_removal(client, session_factory):
    hid, drv = _ready(session_factory)
    client.delete(f"/hosts/{hid}")
    drain(session_factory, drv)
    r = client.post("/hosts", json={"name": "a", "libvirt_uri": URI})
    assert r.status_code == 202
    assert [h["status"] for h in client.get("/hosts").json()] == ["registering"]
