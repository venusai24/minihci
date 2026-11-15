import io
import json

from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings


def test_api_logs_json_with_task_enqueued(session_factory):
    stream = io.StringIO()
    client = TestClient(create_app(Settings(), session_factory=session_factory, log_stream=stream))
    r = client.post("/hosts", json={"name": "a", "libvirt_uri": "qemu+ssh://a/system"})
    tid = r.json()["task_id"]
    lines = [json.loads(x) for x in stream.getvalue().splitlines() if x.strip()]
    assert any(l["msg"] == "task_enqueued" and l["task_id"] == tid for l in lines)
    req = [l for l in lines if l["msg"] == "request"]
    assert req and req[-1]["method"] == "POST" and req[-1]["path"] == "/hosts" and req[-1]["status"] == 202
    assert "ms" in req[-1]
