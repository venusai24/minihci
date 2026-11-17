from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from minihci.api import dr as dr_api
from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Host, ProtectionPolicy, Replica, Task, Vm
from tests.helpers import add_ready_host, drain
from tests.test_vm_power import make_vm

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
BODY = {"interval_s": 60, "retention": 3, "enabled": True}


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(Settings(), session_factory=session_factory))


@pytest.fixture
def host(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def vm(session_factory, driver, host):
    return make_vm(session_factory, driver, host)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(dr_api, "_now", lambda: NOW)


def policy(session_factory, vid, interval=60, retention=3, enabled=True):
    with session_factory() as s, s.begin():
        s.add(ProtectionPolicy(vm_id=vid, interval_s=interval, retention=retention, enabled=enabled))


def replica(session_factory, vid, snap_ago, took=5, status="succeeded", mode="full", seq=0, chain=None, size=None):
    with session_factory() as s, s.begin():
        host_id = s.scalars(select(Host.id)).first()
        t = Task(type="vm.replicate", target_id=vid, state="succeeded")
        s.add(t)
        s.flush()
        snap = NOW - timedelta(seconds=snap_ago)
        r = Replica(
            vm_id=vid,
            task_id=t.id,
            host_id=host_id,
            mode=mode,
            status=status,
            seq=seq,
            chain_id=chain,
            snapshot_time=snap,
            finished_at=snap + timedelta(seconds=took),
            size_bytes=size,
            dr_path=f"/dr/{t.id}.qcow2",
        )
        s.add(r)
        s.flush()
        return r.id


def test_put_protection_202_and_applied(client, session_factory, driver, vm):
    r = client.put(f"/vms/{vm}/protection", json=BODY, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 202
    tid = r.json()["task_id"]
    assert r.headers["location"] == f"/tasks/{tid}"
    drain(session_factory, driver)
    with session_factory() as s:
        p = s.get(ProtectionPolicy, vm)
        assert (p.interval_s, p.retention, p.enabled) == (60, 3, True)
        assert s.get(Task, tid).state == "succeeded"
    r2 = client.put(f"/vms/{vm}/protection", json=BODY, headers={"Idempotency-Key": "k1"})
    assert r2.status_code == 202 and r2.json()["task_id"] == tid
    r3 = client.put(f"/vms/{vm}/protection", json={**BODY, "retention": 4}, headers={"Idempotency-Key": "k1"})
    assert r3.status_code == 409


def test_put_protection_idempotent_same_body_twice(client, session_factory, driver, vm):
    for _ in range(2):
        assert client.put(f"/vms/{vm}/protection", json=BODY).status_code == 202
    drain(session_factory, driver)
    with session_factory() as s:
        assert len(list(s.scalars(select(ProtectionPolicy)))) == 1
        tasks = list(s.scalars(select(Task).where(Task.type == "vm.protection_set")))
        assert len(tasks) == 2 and all(t.state == "succeeded" for t in tasks)


def test_put_protection_updates_existing(client, session_factory, driver, vm):
    policy(session_factory, vm)
    client.put(f"/vms/{vm}/protection", json={"interval_s": 300, "retention": 9, "enabled": False})
    drain(session_factory, driver)
    with session_factory() as s:
        p = s.get(ProtectionPolicy, vm)
        assert (p.interval_s, p.retention, p.enabled) == (300, 9, False)


@pytest.mark.parametrize("body", [{"interval_s": 29, "retention": 3}, {"interval_s": 60, "retention": 0}])
def test_put_protection_validation_422(client, vm, body):
    assert client.put(f"/vms/{vm}/protection", json=body).status_code == 422


def test_put_protection_404_missing_and_absent(client, session_factory, vm):
    assert client.put("/vms/9999/protection", json=BODY).status_code == 404
    with session_factory() as s, s.begin():
        s.get(Vm, vm).desired_state = "absent"
    assert client.put(f"/vms/{vm}/protection", json=BODY).status_code == 404


def test_put_protection_dr_site_vm_409(client, session_factory, vm):
    with session_factory() as s, s.begin():
        s.scalars(select(Host)).first().site = "dr"
    r = client.put(f"/vms/{vm}/protection", json=BODY)
    assert r.status_code == 409
    assert r.json()["detail"] == "vm runs at the DR site; protection cannot be changed"


def test_put_protection_409_when_delete_open(client, session_factory, vm):
    with session_factory() as s, s.begin():
        s.add(Task(type="vm.delete", target_id=vm, state="pending"))
    assert client.put(f"/vms/{vm}/protection", json=BODY).status_code == 409


def test_get_protection_status(client, session_factory, vm):
    policy(session_factory, vm, interval=60)
    replica(session_factory, vm, snap_ago=160, took=10)
    replica(session_factory, vm, snap_ago=100, took=10)
    replica(session_factory, vm, snap_ago=40, took=10)
    j = client.get(f"/vms/{vm}/protection").json()
    assert j["vm_id"] == vm and j["interval_s"] == 60 and j["retention"] == 3 and j["enabled"] is True
    assert j["lag_s"] == 40
    assert j["achieved_rpo_s"] == 70  # newest finish minus previous snapshot, worst gap
    assert j["rpo_target_s"] == 90
    assert j["within_target"] is True
    assert j["replica_count"] == 3
    assert j["last_replicated_at"] == "2026-03-01T11:59:20Z"


def test_status_out_of_target(client, session_factory, vm):
    policy(session_factory, vm, interval=60)
    replica(session_factory, vm, snap_ago=500, took=5)
    j = client.get(f"/vms/{vm}/protection").json()
    assert j["lag_s"] == 500 and j["within_target"] is False


def test_status_counts_succeeded_only_and_pruned_feed_history(client, session_factory, vm):
    policy(session_factory, vm, interval=60)
    replica(session_factory, vm, snap_ago=100, status="pruned")
    replica(session_factory, vm, snap_ago=30)
    replica(session_factory, vm, snap_ago=20, status="failed")
    j = client.get(f"/vms/{vm}/protection").json()
    assert j["replica_count"] == 1
    assert j["lag_s"] == 30 and j["achieved_rpo_s"] == 75


def test_status_stale_replica_older_than_window_still_has_lag(client, session_factory, vm):
    policy(session_factory, vm, interval=60)
    replica(session_factory, vm, snap_ago=3 * 86400)
    j = client.get(f"/vms/{vm}/protection").json()
    assert j["replica_count"] == 1
    assert j["lag_s"] == 3 * 86400
    assert j["last_replicated_at"] == "2026-02-26T12:00:00Z"
    assert j["achieved_rpo_s"] is None
    assert j["within_target"] is False


def test_status_window_excludes_old_rows_from_achieved_rpo(client, session_factory, vm):
    policy(session_factory, vm, interval=60)
    replica(session_factory, vm, snap_ago=3 * 86400)  # outside the 24h window: must not set achieved_rpo_s
    replica(session_factory, vm, snap_ago=50, took=5)
    j = client.get(f"/vms/{vm}/protection").json()
    assert j["replica_count"] == 2
    assert j["lag_s"] == 50
    assert j["achieved_rpo_s"] is None  # only one row inside the window
    assert j["within_target"] is True


def test_status_policy_without_replicas(client, session_factory, vm):
    policy(session_factory, vm)
    r = client.get(f"/vms/{vm}/protection")
    assert r.status_code == 200
    j = r.json()
    assert j["last_replicated_at"] is None and j["lag_s"] is None and j["achieved_rpo_s"] is None
    assert j["within_target"] is False and j["replica_count"] == 0


def test_get_protection_404(client, vm):
    assert client.get(f"/vms/{vm}/protection").status_code == 404


def test_replicas_newest_first(client, session_factory, vm):
    a = replica(session_factory, vm, snap_ago=300)
    b = replica(session_factory, vm, snap_ago=100, mode="incremental", seq=1, chain=a)
    rows = client.get(f"/vms/{vm}/replicas").json()
    assert [r["id"] for r in rows] == [b, a]
    assert set(rows[0]) == {
        "id",
        "mode",
        "status",
        "chain_id",
        "seq",
        "snapshot_time",
        "finished_at",
        "duration_ms",
        "size_bytes",
        "dr_path",
    }
    assert rows[0]["snapshot_time"] == "2026-03-01T11:58:20Z"
    assert client.get("/vms/9999/replicas").status_code == 404


def test_replication_lists_all(client, session_factory, driver, host, vm):
    v2 = make_vm(session_factory, driver, host, name="v2")
    v3 = make_vm(session_factory, driver, host, name="v3")
    policy(session_factory, v2)
    policy(session_factory, vm)
    policy(session_factory, v3)
    replica(session_factory, vm, snap_ago=10)
    with session_factory() as s, s.begin():
        s.get(Vm, v3).desired_state = "absent"  # absent VMs are excluded
    rows = client.get("/replication").json()
    assert [r["vm_id"] for r in rows] == sorted([vm, v2])
    assert {r["vm_name"] for r in rows} == {"web1", "v2"}
    assert rows[0]["replica_count"] == 1 and "within_target" in rows[0]


def test_auth_required_when_token_set(session_factory):
    c = TestClient(create_app(Settings(api_token="s3cret"), session_factory=session_factory))
    assert c.get("/replication").status_code == 401
    assert c.put("/vms/1/protection", json=BODY).status_code == 401
    assert c.get("/vms/1/failovers").status_code == 401
    assert c.get("/replication", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_failovers_404_and_empty(client, vm):
    assert client.get("/vms/9999/failovers").status_code == 404
    assert client.get(f"/vms/{vm}/failovers").json() == []
