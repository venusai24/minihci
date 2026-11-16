import json

import httpx
from typer.testing import CliRunner

from minihci import cli

runner = CliRunner()
TASK = {"id": 7, "type": "vm.create", "target_id": 1, "state": "running", "error": None}


def _install(monkeypatch, handler, seen=None):
    def make_client(api):
        if seen is not None:
            seen.append(api)
        return httpx.Client(base_url=api, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli, "make_client", make_client)


def test_tasks_show(monkeypatch):
    reqs = []

    def h(r):
        reqs.append((r.method, r.url.path))
        return httpx.Response(200, json=TASK)

    _install(monkeypatch, h)
    res = runner.invoke(cli.app, ["tasks", "show", "7"])
    assert res.exit_code == 0, res.output
    assert reqs == [("GET", "/tasks/7")]
    assert "running" in res.output


def test_json_output(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json=TASK))
    res = runner.invoke(cli.app, ["--json", "tasks", "show", "7"])
    assert res.exit_code == 0
    assert json.loads(res.stdout) == TASK


def test_tasks_list_state(monkeypatch):
    q = []

    def h(r):
        q.append(dict(r.url.params))
        return httpx.Response(200, json=[TASK])

    _install(monkeypatch, h)
    res = runner.invoke(cli.app, ["tasks", "list", "--state", "running"])
    assert res.exit_code == 0
    assert q == [{"state": "running"}]
    assert "vm.create" in res.output


def test_404_exit_1(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(404, json={"detail": "task not found"}))
    res = runner.invoke(cli.app, ["tasks", "show", "9"])
    assert res.exit_code == 1
    assert "task not found" in res.stderr


def test_connection_error_exit_3(monkeypatch):
    def h(r):
        raise httpx.ConnectError("boom")

    _install(monkeypatch, h)
    res = runner.invoke(cli.app, ["tasks", "show", "1"])
    assert res.exit_code == 3


def test_5xx_exit_3(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(500, text="oops"))
    res = runner.invoke(cli.app, ["tasks", "show", "1"])
    assert res.exit_code == 3


def test_env_and_flag_base_url(monkeypatch):
    seen = []
    _install(monkeypatch, lambda r: httpx.Response(200, json=TASK), seen)
    monkeypatch.setenv("MINIHCI_API", "http://envhost:1")
    runner.invoke(cli.app, ["tasks", "show", "7"])
    runner.invoke(cli.app, ["--api", "http://flag:2", "tasks", "show", "7"])
    assert seen == ["http://envhost:1", "http://flag:2"]


def test_default_base_url(monkeypatch):
    seen = []
    _install(monkeypatch, lambda r: httpx.Response(200, json=TASK), seen)
    monkeypatch.delenv("MINIHCI_API", raising=False)
    runner.invoke(cli.app, ["tasks", "show", "7"])
    assert seen == ["http://localhost:8000"]
