import signal

import pytest
from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.models import Vm
from tests.helpers import add_ready_host, load_task, spawn_worker, wait_for


@pytest.mark.slow
@pytest.mark.timeout(60)
def test_worker_reconciler_marks_deleted_domain_missing(session_factory, tmp_path):
    state = tmp_path / "fake.json"
    driver = FakeDriver(state_file=state)
    add_ready_host(session_factory, driver)
    client = TestClient(create_app(Settings(storage_root="/srv/test-images"), session_factory=session_factory))
    proc = spawn_worker(state, {"MINIHCI_RECONCILE_INTERVAL_S": "0.5"})
    try:
        r = client.post(
            "/vms",
            json={"name": "web1", "host_id": 1, "vcpus": 1, "mem_mb": 512, "image": "/srv/minihci/base/alpine.qcow2"},
        )
        tid = r.json()["task_id"]
        wait_for(lambda: load_task(session_factory, tid).state == "succeeded", timeout=15)
        # remove the domain behind the platform's back (state file edit under the fake's own lock)
        with driver._locked() as st:
            for h in st["hosts"].values():
                h["domains"].pop("web1", None)

        def missing():
            with session_factory() as s:
                return s.get(Vm, 1).actual_state == "missing"

        wait_for(missing, timeout=15)
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0, proc.stderr.read().decode()[-2000:]
