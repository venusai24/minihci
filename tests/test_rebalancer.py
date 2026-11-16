import pytest
from sqlalchemy import select, text

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Task
from minihci.scheduler.collector import collect_once
from minihci.scheduler.rebalancer import rebalance_once
from tests.helpers import add_ready_host, drain
from tests.test_vm_power import URI, make_vm

URI_B = "qemu+ssh://b/system"
S = Settings()


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def ha(session_factory, driver):
    return add_ready_host(session_factory, driver)


@pytest.fixture
def hb(session_factory, driver):
    return add_ready_host(session_factory, driver, name="b", uri=URI_B)


def configure(session_factory, **kw):
    cols = ", ".join(kw)
    vals = ", ".join(f":{k}" for k in kw)
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in kw)
    with session_factory() as s, s.begin():  # tests truncate scheduler_config, so upsert row 1
        s.execute(
            text(f"INSERT INTO scheduler_config (id, {cols}) VALUES (1, {vals}) ON CONFLICT (id) DO UPDATE SET {sets}"),
            kw,
        )


def collect(session_factory, driver, n=2):
    for _ in range(n):
        collect_once(session_factory, driver, S)


def hot(session_factory, driver, a=90, b=10, n=2):
    driver.set_load(URI, a)
    driver.set_load(URI_B, b)
    collect(session_factory, driver, n)


def migrate_tasks(session_factory):
    with session_factory() as s:
        return list(s.scalars(select(Task).where(Task.type == "vm.migrate").order_by(Task.id)))


@pytest.fixture
def setup(session_factory, driver, ha, hb):
    configure(session_factory, rebalance_enabled=True)
    v1 = make_vm(session_factory, driver, ha, name="v1")
    v2 = make_vm(session_factory, driver, ha, name="v2")
    return v1, v2


def test_enqueues_exactly_one(session_factory, driver, ha, hb, setup):
    hot(session_factory, driver)
    move = rebalance_once(session_factory, S)
    assert move is not None and move.src_host_id == ha and move.dst_host_id == hb
    (t,) = migrate_tasks(session_factory)
    assert t.state == "pending" and t.target_id == move.vm_id
    assert t.params == {"src_host_id": ha, "dst_host_id": hb, "reason": "rebalance"}
    assert rebalance_once(session_factory, S) is None
    assert len(migrate_tasks(session_factory)) == 1


def test_cooldown_after_drain(session_factory, driver, ha, hb, setup):
    make_vm(session_factory, driver, ha, name="v3")
    hot(session_factory, driver)
    assert rebalance_once(session_factory, S) is not None
    drain(session_factory, driver)
    hot(session_factory, driver)
    assert rebalance_once(session_factory, S) is None  # host cooldown (the other VM would be eligible)
    configure(session_factory, rebalance_host_cooldown_s=0)
    move = rebalance_once(session_factory, S)  # host cooldown off: the other VM moves, not the cooled one
    assert move is not None


def test_vm_cooldown_blocks_recent_vm(session_factory, driver, ha, hb):
    from minihci.tasks.engine import enqueue

    configure(session_factory, rebalance_enabled=True, rebalance_host_cooldown_s=0)
    v1 = make_vm(session_factory, driver, hb, name="v1")
    make_vm(session_factory, driver, hb, name="v2")
    make_vm(session_factory, driver, hb, name="v3")
    with session_factory() as s, s.begin():
        s.execute(text("UPDATE vms SET mem_mb = 256 WHERE id = :i"), {"i": v1})  # v1 is the cheapest
        past = enqueue(s, "vm.migrate", v1, {"src_host_id": ha, "dst_host_id": hb})
        past_id = past.id
        s.execute(text("UPDATE tasks SET state = 'succeeded' WHERE id = :i"), {"i": past_id})
    hot(session_factory, driver, a=10, b=90)
    move = rebalance_once(session_factory, S)
    assert move is not None and move.vm_id != v1
    with session_factory() as s, s.begin():
        s.execute(text("DELETE FROM tasks WHERE type = 'vm.migrate' AND id != :i"), {"i": past_id})
    configure(session_factory, rebalance_vm_cooldown_s=0)
    move = rebalance_once(session_factory, S)
    assert move is not None and move.vm_id == v1


def test_disabled(session_factory, driver, setup):
    configure(session_factory, rebalance_enabled=False)
    hot(session_factory, driver)
    assert rebalance_once(session_factory, S) is None
    assert migrate_tasks(session_factory) == []


def test_below_threshold(session_factory, driver, setup):
    hot(session_factory, driver, a=70, b=10)
    assert rebalance_once(session_factory, S) is None


def test_one_sample(session_factory, driver, setup):
    hot(session_factory, driver, n=1)
    assert rebalance_once(session_factory, S) is None


def test_other_migrate_in_flight(session_factory, driver, ha, hb, setup):
    from minihci.tasks.engine import enqueue

    hot(session_factory, driver)
    with session_factory() as s, s.begin():
        enqueue(s, "vm.migrate", setup[0], {"src_host_id": ha, "dst_host_id": hb})
    assert rebalance_once(session_factory, S) is None
