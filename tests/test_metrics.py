import ast
from datetime import UTC
from pathlib import Path

from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from minihci.app import create_app
from minihci.config import Settings
from minihci.driver.fake import FakeDriver
from minihci.tasks.registry import REGISTRY
from minihci.tasks.worker import Worker


def _samples(text: str, family: str) -> dict[tuple, float]:
    out = {}
    for fam in text_string_to_metric_families(text):
        if fam.name == family:
            for s in fam.samples:
                out[tuple(sorted(s.labels.items()))] = s.value
    return out


def test_metrics_task_counts(session_factory):
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    for n in ("a", "b"):
        assert client.post("/hosts", json={"name": n, "libvirt_uri": f"qemu+ssh://{n}/system"}).status_code == 202
    driver = FakeDriver()
    driver.add_host("qemu+ssh://a/system", 8, 16384)
    driver.add_host("qemu+ssh://b/system", 8, 16384)
    Worker(session_factory, driver, Settings(), registry=REGISTRY).run_once()
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    got = _samples(r.text, "minihci_tasks")
    assert got[(("state", "succeeded"), ("type", "host.register"))] == 1
    assert got[(("state", "pending"), ("type", "host.register"))] == 1


def test_api_modules_do_not_import_driver():
    for path in Path("src/minihci/api").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            assert not any(n.startswith("minihci.driver") for n in names), path


def test_metrics_task_duration_histogram(session_factory):
    from datetime import datetime, timedelta

    from minihci.models import Task

    t0 = datetime(2026, 1, 1)
    durations = [
        ("host.register", "succeeded", 0.05),
        ("host.register", "failed", 0.7),
        ("host.register", "rolled_back", 20.0),
        ("host.register", "running", 3.0),
        ("vm.create", "succeeded", 400.0),
    ]
    with session_factory() as s:
        for i, (typ, state, d) in enumerate(durations):
            s.add(
                Task(
                    type=typ,
                    target_id=i,
                    state=state,
                    started_at=t0,
                    finished_at=t0 + timedelta(seconds=d) if state != "running" else None,
                )
            )
        s.commit()
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    text = client.get("/metrics").text
    got = _samples(text, "minihci_task_duration_seconds")

    def b(typ, le):
        return got[tuple(sorted({"type": typ, "le": le}.items()))]

    assert b("host.register", "0.1") == 1
    assert b("host.register", "0.5") == 1
    assert b("host.register", "1.0") == 2
    assert b("host.register", "15.0") == 2
    assert b("host.register", "60.0") == 3
    assert b("host.register", "+Inf") == 3
    fams = {f.name: f for f in text_string_to_metric_families(text)}
    samples = {
        (s.name, tuple(sorted(s.labels.items()))): s.value for s in fams["minihci_task_duration_seconds"].samples
    }
    assert samples[("minihci_task_duration_seconds_count", (("type", "host.register"),))] == 3
    assert abs(samples[("minihci_task_duration_seconds_sum", (("type", "host.register"),))] - 20.75) < 1e-6
    assert b("vm.create", "300.0") == 0
    assert b("vm.create", "+Inf") == 1


def test_metrics_host_status_and_heartbeat(session_factory):
    from datetime import datetime

    from minihci.models import Host

    hb = datetime(2026, 1, 1, tzinfo=UTC)
    with session_factory() as s:
        s.add(Host(name="a", libvirt_uri="u://a", status="ready", last_heartbeat=hb))
        s.add(Host(name="b", libvirt_uri="u://b", status="failed"))
        s.add(Host(name="c", libvirt_uri="u://c", status="removed"))
        s.commit()
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    text = client.get("/metrics").text
    st = _samples(text, "minihci_host_status")
    assert st[(("host", "a"), ("status", "ready"))] == 1
    assert st[(("host", "a"), ("status", "failed"))] == 0
    assert st[(("host", "b"), ("status", "failed"))] == 1
    assert not any(k[0] == ("host", "c") for k in st)
    h = _samples(text, "minihci_host_last_heartbeat_timestamp_seconds")
    assert h == {(("host", "a"),): hb.timestamp()}


def test_metrics_migration_families_present_empty(session_factory):
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    text = client.get("/metrics").text
    fams = {f.name: f for f in text_string_to_metric_families(text)}
    for name in ("minihci_migration_duration_seconds", "minihci_migration_downtime_seconds"):
        assert fams[name].type == "histogram"
        counts = [s.value for s in fams[name].samples if s.name.endswith("_count")]
        assert counts == [0]


def _hist(text: str, family: str) -> dict[str, float]:
    fams = {f.name: f for f in text_string_to_metric_families(text)}
    out = {}
    for s in fams[family].samples:
        out[s.labels.get("le", s.name.rsplit("_", 1)[-1])] = s.value
    return out


def test_metrics_migration_histograms_filled(session_factory):
    from minihci.models import Host, Migration, Task, Vm

    rows = [("succeeded", 400, 30), ("succeeded", 3000, 150), ("succeeded", None, None), ("failed", 99000, 99000)]
    with session_factory() as s:
        h1 = Host(name="h1", libvirt_uri="u://1", status="ready")
        h2 = Host(name="h2", libvirt_uri="u://2", status="ready")
        s.add_all([h1, h2])
        s.flush()
        for i, (status, dur, down) in enumerate(rows):
            vm = Vm(name=f"v{i}", host_id=h1.id, vcpus=1, mem_mb=128, image="i", disk_path=f"/d{i}")
            t = Task(type="vm.migrate", target_id=i)
            s.add_all([vm, t])
            s.flush()
            s.add(
                Migration(
                    task_id=t.id,
                    vm_id=vm.id,
                    src_host_id=h1.id,
                    dst_host_id=h2.id,
                    status=status,
                    duration_ms=dur,
                    downtime_ms=down,
                )
            )
        s.commit()
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    text = client.get("/metrics").text
    d = _hist(text, "minihci_migration_duration_seconds")
    assert d["0.25"] == 0 and d["0.5"] == 1 and d["2.0"] == 1 and d["5.0"] == 2 and d["+Inf"] == 2
    assert d["count"] == 2 and abs(d["sum"] - 3.4) < 1e-9
    w = _hist(text, "minihci_migration_downtime_seconds")
    assert w["0.05"] == 1
    assert w["0.1"] == 1 and w["0.25"] == 2 and w["+Inf"] == 2
    assert w["count"] == 2 and abs(w["sum"] - 0.18) < 1e-9


def test_metrics_migration_histograms_independent_nulls(session_factory):
    from minihci.models import Host, Migration, Task, Vm

    with session_factory() as s:
        h1 = Host(name="h1", libvirt_uri="u://1", status="ready")
        h2 = Host(name="h2", libvirt_uri="u://2", status="ready")
        s.add_all([h1, h2])
        s.flush()
        vm = Vm(name="v0", host_id=h1.id, vcpus=1, mem_mb=128, image="i", disk_path="/d0")
        t = Task(type="vm.migrate", target_id=0)
        s.add_all([vm, t])
        s.flush()
        s.add(
            Migration(
                vm_id=vm.id,
                src_host_id=h1.id,
                dst_host_id=h2.id,
                task_id=t.id,
                status="succeeded",
                duration_ms=500,
                downtime_ms=None,
            )
        )
        s.commit()
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    text = client.get("/metrics").text
    d = _hist(text, "minihci_migration_duration_seconds")
    assert d["count"] == 1 and abs(d["sum"] - 0.5) < 1e-9 and d["0.5"] == 1
    w = _hist(text, "minihci_migration_downtime_seconds")
    assert w["count"] == 0 and w["sum"] == 0
