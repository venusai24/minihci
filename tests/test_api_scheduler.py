from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings


def test_get_scheduler_config_default(session_factory):
    client = TestClient(create_app(Settings(), session_factory=session_factory))
    r = client.get("/scheduler/config")
    assert r.status_code == 200
    assert r.json() == {"cpu_headroom_pct": 80}
