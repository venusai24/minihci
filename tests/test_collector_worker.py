import signal

import pytest
from sqlalchemy import select

from minihci.driver.fake import FakeDriver
from minihci.models import HostMetric
from tests.helpers import add_ready_host, spawn_worker, wait_for

URI = "qemu+ssh://a/system"


@pytest.mark.slow
@pytest.mark.timeout(60)
def test_worker_collector_stores_metrics(session_factory, tmp_path):
    state = tmp_path / "fake.json"
    driver = FakeDriver(state_file=state)
    host_id = add_ready_host(session_factory, driver, "a", URI)
    driver.set_load(URI, 42.5)
    proc = spawn_worker(state, {"MINIHCI_COLLECTOR_INTERVAL_S": "0.3"})
    try:

        def enough():
            with session_factory() as s:
                rows = s.scalars(select(HostMetric).where(HostMetric.host_id == host_id)).all()
                return len(rows) >= 2 and all(r.cpu_pct == 42.5 for r in rows)

        wait_for(enough, timeout=20)
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0, proc.stderr.read().decode()[-2000:]
