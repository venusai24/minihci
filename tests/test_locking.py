import threading
import time

from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.tasks.engine import enqueue
from minihci.tasks.executor import Step, TaskType
from minihci.tasks.worker import Worker
from tests.helpers import load_task


def sleeper(log, name="test.sleep", kind="vm", fail=False):
    def body(ctx):
        log.append("start")
        time.sleep(0.4)
        log.append("end")
        if fail:
            raise RuntimeError("fail after sleep")
    return TaskType(name, kind, [Step("sleep", body)])


def run_concurrently(session_factory, jobs):
    """jobs: list of (TaskType, target_id). One Worker thread per job."""
    registry = {tt.name: tt for tt, _ in jobs}
    with session_factory() as s:
        for tt, target in jobs:
            enqueue(s, tt.name, target, {})
        s.commit()
    workers = [Worker(session_factory, FakeDriver(), Settings(), registry=registry) for _ in jobs]
    threads = [threading.Thread(target=w.run_once) for w in workers]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=10)


def test_same_vm_tasks_serialize(session_factory):
    log = []
    tt = sleeper(log)
    run_concurrently(session_factory, [(tt, 5), (tt, 5)])
    assert log == ["start", "end", "start", "end"]


def test_different_vms_run_in_parallel(session_factory):
    log = []
    tt = sleeper(log)
    run_concurrently(session_factory, [(tt, 5), (tt, 6)])
    assert log == ["start", "start", "end", "end"]


def test_vm_and_host_namespaces_do_not_collide(session_factory):
    log = []
    run_concurrently(session_factory, [(sleeper(log, "test.vm", "vm"), 5), (sleeper(log, "test.host", "host"), 5)])
    assert log == ["start", "start", "end", "end"]


def test_lock_released_after_failed_task(session_factory):
    log = []
    bad, good = sleeper(log, "test.bad", fail=True), sleeper(log, "test.good")
    run_concurrently(session_factory, [(bad, 5), (good, 5)])
    assert log == ["start", "end", "start", "end"]
    assert {load_task(session_factory, 1).state, load_task(session_factory, 2).state} == {"rolled_back", "succeeded"}


def test_run_once_idle_returns_none(session_factory):
    assert Worker(session_factory, FakeDriver(), Settings(), registry={}).run_once() is None
