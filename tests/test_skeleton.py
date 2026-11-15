import io
import json
import logging

from fastapi.testclient import TestClient

from minihci.app import create_app
from minihci.config import Settings
from minihci.logging import configure_logging, task_context


def test_healthz():
    client = TestClient(create_app(Settings(database_url="postgresql+psycopg://x@localhost/none")))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_settings_read_env(monkeypatch):
    monkeypatch.setenv("MINIHCI_DATABASE_URL", "postgresql+psycopg://u:p@h:1/db")
    monkeypatch.setenv("MINIHCI_FAKE_DELAYS", '{"define_domain": 2.5}')
    s = Settings()
    assert s.database_url == "postgresql+psycopg://u:p@h:1/db"
    assert s.driver == "fake"
    assert s.fake_delays == {"define_domain": 2.5}
    assert s.max_attempts == 3


def test_json_logs_carry_task_id():
    buf = io.StringIO()
    configure_logging(stream=buf)
    log = logging.getLogger("minihci.test")
    with task_context(42):
        log.info("step done")
    log.info("outside")
    lines = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert lines[0]["msg"] == "step done"
    assert lines[0]["task_id"] == 42
    assert lines[0]["level"] == "INFO"
    assert {"ts", "logger"} <= set(lines[0])
    assert lines[1]["task_id"] is None
