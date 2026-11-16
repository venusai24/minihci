from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings

DEFAULTS = {
    "cpu_headroom_pct": 80,
    "rebalance_enabled": False,
    "rebalance_cpu_threshold_pct": 80,
    "rebalance_window_s": 300,
    "rebalance_vm_cooldown_s": 1800,
    "rebalance_host_cooldown_s": 600,
}


def test_get_scheduler_config_default(session_factory):
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    r = client.get("/scheduler/config")
    assert r.status_code == 200
    assert r.json() == {**DEFAULTS, "cpu_headroom_pct": 80}


# --- PUT /scheduler/config (Q06.5) ---
import pytest

from minihci.driver.fake import FakeDriver
from tests.helpers import drain, load_task


@pytest.fixture
def client(session_factory):
    return TestClient(create_app(Settings(), session_factory=session_factory))


def _host(session_factory):
    from minihci.models import Host

    with session_factory() as s:
        s.add(Host(name="h", libvirt_uri="qemu+ssh://h/system", status="ready", cpu_total=10, mem_total_mb=65536))
        s.commit()


def _vm(**kw):
    return {"name": "a1", "vcpus": 6, "mem_mb": 512, "image": "/srv/minihci/base/alpine.qcow2"} | kw


def test_put_returns_202_and_task_row(client, session_factory):
    r = client.put("/scheduler/config", json={"cpu_headroom_pct": 50})
    assert r.status_code == 202
    t = load_task(session_factory, r.json()["task_id"])
    assert t.type == "scheduler.config_set" and t.target_id == 1 and t.state == "pending"


@pytest.mark.parametrize("v", [0, 101, -5])
def test_put_out_of_range_422(client, v):
    assert client.put("/scheduler/config", json={"cpu_headroom_pct": v}).status_code == 422


def test_put_applies_and_changes_placement(client, session_factory):
    _host(session_factory)
    r = client.post("/vms", json=_vm())  # 6 <= 80% of 10
    assert r.status_code == 202
    drain(session_factory, FakeDriver())
    from sqlalchemy import text

    with session_factory() as s:
        s.execute(text("DELETE FROM vms"))
        s.commit()
    assert client.put("/scheduler/config", json={"cpu_headroom_pct": 50}).status_code == 202
    drain(session_factory, FakeDriver())
    assert client.get("/scheduler/config").json() == {**DEFAULTS, "cpu_headroom_pct": 50}
    assert client.post("/vms", json=_vm()).status_code == 409


def test_put_upserts_when_row_missing(client, session_factory):
    from sqlalchemy import text

    with session_factory() as s:
        s.execute(text("TRUNCATE scheduler_config"))
        s.commit()
    r = client.put("/scheduler/config", json={"cpu_headroom_pct": 60})
    drain(session_factory, FakeDriver())
    assert load_task(session_factory, r.json()["task_id"]).state == "succeeded"
    assert client.get("/scheduler/config").json() == {**DEFAULTS, "cpu_headroom_pct": 60}


def test_put_replay(client):
    h = {"Idempotency-Key": "sc-k"}
    a = client.put("/scheduler/config", json={"cpu_headroom_pct": 50}, headers=h)
    b = client.put("/scheduler/config", json={"cpu_headroom_pct": 50}, headers=h)
    assert b.status_code == 202 and b.json()["task_id"] == a.json()["task_id"]
    assert client.put("/scheduler/config", json={"cpu_headroom_pct": 70}, headers=h).status_code == 409


# --- rebalance settings (Q09.1) ---
def test_put_rebalance_round_trip(client, session_factory):
    body = {
        "cpu_headroom_pct": 70,
        "rebalance_enabled": True,
        "rebalance_cpu_threshold_pct": 90,
        "rebalance_window_s": 60,
        "rebalance_vm_cooldown_s": 0,
        "rebalance_host_cooldown_s": 10,
    }
    assert client.put("/scheduler/config", json=body).status_code == 202
    drain(session_factory, FakeDriver())
    assert client.get("/scheduler/config").json() == body


def test_put_only_headroom_keeps_rebalance_settings(client, session_factory):
    client.put("/scheduler/config", json={"cpu_headroom_pct": 80, "rebalance_enabled": True, "rebalance_window_s": 90})
    drain(session_factory, FakeDriver())
    client.put("/scheduler/config", json={"cpu_headroom_pct": 60})
    drain(session_factory, FakeDriver())
    got = client.get("/scheduler/config").json()
    assert got["cpu_headroom_pct"] == 60 and got["rebalance_enabled"] is True and got["rebalance_window_s"] == 90


@pytest.mark.parametrize(
    "k,v",
    [
        ("rebalance_cpu_threshold_pct", 0),
        ("rebalance_cpu_threshold_pct", 101),
        ("rebalance_window_s", 29),
        ("rebalance_vm_cooldown_s", -1),
        ("rebalance_host_cooldown_s", -1),
        ("rebalance_enabled", "maybe"),
    ],
)
def test_put_rebalance_out_of_range_422(client, k, v):
    assert client.put("/scheduler/config", json={"cpu_headroom_pct": 80, k: v}).status_code == 422


def test_put_only_rebalance_enabled_keeps_headroom(client, session_factory):
    client.put("/scheduler/config", json={"cpu_headroom_pct": 60})
    drain(session_factory, FakeDriver())
    assert client.put("/scheduler/config", json={"rebalance_enabled": True}).status_code == 202
    drain(session_factory, FakeDriver())
    got = client.get("/scheduler/config").json()
    assert got["cpu_headroom_pct"] == 60 and got["rebalance_enabled"] is True


def test_put_empty_body_422(client):
    assert client.put("/scheduler/config", json={}).status_code == 422
