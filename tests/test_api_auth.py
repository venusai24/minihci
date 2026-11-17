import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from minihci import cli
from minihci.app import create_app
from minihci.config import Settings

runner = CliRunner()
PATHS = ["/hosts", "/vms", "/tasks", "/metrics"]


def _client(session_factory, token):
    return TestClient(create_app(Settings(api_token=token), session_factory=session_factory))


def test_open_when_no_token_configured(session_factory):
    c = _client(session_factory, None)
    assert all(c.get(p).status_code == 200 for p in PATHS)


def test_token_required_on_every_api_route(session_factory):
    c = _client(session_factory, "s3cret")
    for p in PATHS:
        r = c.get(p)
        assert r.status_code == 401, p
        assert r.headers["www-authenticate"] == "Bearer"
    assert c.post("/vms", json={}).status_code == 401  # rejected before body validation


def test_wrong_and_malformed_tokens_rejected(session_factory):
    c = _client(session_factory, "s3cret")
    for h in ("Bearer nope", "s3cret", "Basic s3cret", "Bearer "):
        assert c.get("/hosts", headers={"Authorization": h}).status_code == 401, h


def test_right_token_accepted_and_healthz_stays_open(session_factory):
    c = _client(session_factory, "s3cret")
    assert c.get("/hosts", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.get("/healthz").status_code == 200


def test_empty_token_setting_means_open(session_factory):
    assert _client(session_factory, "").get("/hosts").status_code == 200


def test_cli_sends_the_token(monkeypatch):
    seen = []

    def handler(r):
        seen.append(r.headers.get("authorization"))
        return httpx.Response(
            200, json={"id": 7, "type": "vm.create", "target_id": 1, "state": "running", "error": None}
        )

    monkeypatch.setattr(
        cli, "make_client", lambda api: httpx.Client(base_url=api, transport=httpx.MockTransport(handler))
    )
    runner.invoke(cli.app, ["--token", "s3cret", "tasks", "show", "7"])
    runner.invoke(cli.app, ["tasks", "show", "7"])
    assert seen == ["Bearer s3cret", None]
