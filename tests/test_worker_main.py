import signal

import pytest
from sqlalchemy import func

from minihci.driver.fake import FakeDriver
from minihci.models import Host, Task
from minihci.tasks.engine import enqueue
from minihci.worker_main import acquire_singleton
from tests.helpers import load_task, spawn_worker, wait_for

URI = "qemu+ssh://a/system"


def test_singleton_lock_is_exclusive(engine):
    a = acquire_singleton(engine)
    assert a is not None
    assert acquire_singleton(engine) is None
    a.close()
    b = acquire_singleton(engine)
    assert b is not None
    b.close()


def test_signal_handlers_installed_before_recovery(monkeypatch):
    from types import SimpleNamespace

    from minihci import worker_main

    class Closeable:
        def close(self):
            pass

    seen = {}

    class Abort(Exception):
        pass

    def fake_recover(*_a, **_k):
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["int"] = signal.getsignal(signal.SIGINT)
        raise Abort

    old_term = signal.getsignal(signal.SIGTERM)
    old_int = signal.getsignal(signal.SIGINT)
    monkeypatch.setattr(worker_main, "Settings", lambda: SimpleNamespace(database_url="x"))
    monkeypatch.setattr(worker_main, "configure_logging", lambda: None)
    monkeypatch.setattr(worker_main, "make_engine", lambda _u: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(worker_main, "acquire_singleton", lambda _e: Closeable())
    monkeypatch.setattr(worker_main, "make_session_factory", lambda _e: None)
    monkeypatch.setattr(worker_main, "build_driver", lambda _s: None)
    monkeypatch.setattr(worker_main, "recover_orphans", fake_recover)
    try:
        with pytest.raises(Abort):
            worker_main.main()
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
    assert seen["term"] is not old_term and callable(seen["term"])
    assert seen["int"] is not old_int and callable(seen["int"])


@pytest.mark.timeout(30)
def test_process_recovers_orphans_and_drains_queue(session_factory, tmp_path):
    state = tmp_path / "fake.json"
    FakeDriver(state_file=state).add_host(URI, 8, 16384)
    with session_factory() as s:
        h1 = Host(name="a", libvirt_uri=URI)
        s.add(h1)
        s.flush()
        orphan = Task(
            type="host.register", target_id=h1.id, params={}, state="running", attempts=1, started_at=func.now()
        )
        s.add(orphan)
        s.commit()
        orphan_id = orphan.id
    proc = spawn_worker(state)
    try:
        wait_for(lambda: load_task(session_factory, orphan_id).state == "succeeded", timeout=15)
        with session_factory() as s:
            fresh = enqueue(s, "host.register", h1.id, {})
            s.commit()
        wait_for(lambda: load_task(session_factory, fresh.id).state == "succeeded", timeout=15)
    finally:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0, proc.stderr.read().decode()[-2000:]
