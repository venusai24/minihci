from minihci.models import Task
from minihci.tasks.executor import Step, TaskType


def make_three_step_type(
    calls: list, fail_at: str | None = None, undo_fails: bool = False, name: str = "test.three"
) -> TaskType:
    def do(step):
        def f(ctx):
            calls.append(("do", step))
            if step == fail_at:
                raise RuntimeError(f"boom in {step}")

        return f

    def undo(step):
        def f(ctx):
            calls.append(("undo", step))
            if undo_fails:
                raise RuntimeError(f"undo broke {step}")

        return f

    return TaskType(name=name, target_kind="vm", steps=[Step(n, do(n), undo(n)) for n in ("a", "b", "c")])


def step_rows(*pairs: tuple[str, str]) -> list[dict]:
    return [{"name": n, "status": st, "started_at": None, "finished_at": None, "error": None} for n, st in pairs]


def load_task(session_factory, task_id: int) -> Task:
    with session_factory() as s:
        return s.get(Task, task_id)


from minihci.config import Settings
from minihci.tasks.registry import REGISTRY
from minihci.tasks.worker import Worker


def drain(session_factory, driver, registry=None, settings=None) -> list[int]:
    w = Worker(session_factory, driver, settings or Settings(), registry=REGISTRY if registry is None else registry)
    done = []
    while (tid := w.run_once()) is not None:
        done.append(tid)
    return done


from sqlalchemy import select

from minihci.driver.base import DomainState
from minihci.models import Host, Vm


def add_ready_host(session_factory, driver, name="a", uri="qemu+ssh://a/system") -> int:
    driver.add_host(uri, 8, 16384)
    with session_factory() as s:
        h = Host(name=name, libvirt_uri=uri, status="ready", cpu_total=8, mem_total_mb=16384)
        s.add(h)
        s.commit()
        return h.id


def assert_db_matches_driver(session_factory, driver) -> None:
    with session_factory() as s:
        live = s.scalars(select(Task).where(Task.state.in_(["pending", "running"]))).all()
        assert live == [], f"unfinished tasks: {[t.id for t in live]}"
        hosts = {h.id: h for h in s.scalars(select(Host))}
        vms = s.scalars(select(Vm)).all()
    tracked = set()
    live_names = {(hosts[v.host_id].libvirt_uri, v.name): v.disk_path for v in vms if v.actual_state != "absent"}
    for vm in vms:
        uri = hosts[vm.host_id].libvirt_uri
        state = driver.domain_state(uri, vm.name)
        if vm.actual_state == "running":
            assert state == DomainState.RUNNING, f"{vm.name}: db running, driver {state}"
            assert driver.disk_exists(vm.disk_path), f"{vm.name}: disk missing"
            tracked.add((uri, vm.name))
        elif vm.actual_state == "absent":
            if (uri, vm.name) in live_names:  # name reused by a live VM; its row covers the domain state
                if vm.disk_path != live_names[(uri, vm.name)]:
                    assert not driver.disk_exists(vm.disk_path), f"{vm.name}: leaked disk"
                continue
            assert state is None, f"{vm.name}: db absent, driver {state}"
            assert not driver.disk_exists(vm.disk_path), f"{vm.name}: leaked disk"
        else:
            raise AssertionError(f"{vm.name} left in actual_state={vm.actual_state}")
    for h in hosts.values():
        for name in driver.list_domains(h.libvirt_uri):
            assert (h.libvirt_uri, name) in tracked, f"untracked domain {name} on {h.libvirt_uri}"


import os
import subprocess
import sys
import time
from pathlib import Path

from tests.dbutil import TEST_DB_URL


def spawn_worker(state_file: Path, env: dict[str, str] | None = None) -> subprocess.Popen:
    full = (
        os.environ
        | {
            "MINIHCI_DATABASE_URL": TEST_DB_URL,
            "MINIHCI_DRIVER": "fake",
            "MINIHCI_FAKE_STATE_FILE": str(state_file),
            "MINIHCI_WORKER_POLL_INTERVAL_S": "0.05",
        }
        | (env or {})
    )
    return subprocess.Popen(
        [sys.executable, "-m", "minihci.worker_main"], env=full, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )


def wait_for(predicate, timeout: float, interval: float = 0.1) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")
