import ast
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
