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


def _seq(states, calls=None):
    it = iter(states)
    last = {}

    def h(r):
        if calls is not None:
            calls.append((r.method, r.url.path))
        nonlocal last
        try:
            last = next(it)
        except StopIteration:
            pass
        return httpx.Response(200, json=last)

    return h


def test_wait_succeeds(monkeypatch):
    calls = []
    _install(monkeypatch, _seq([{**TASK, "state": s} for s in ("pending", "running", "succeeded")], calls))
    res = runner.invoke(cli.app, ["tasks", "wait", "7", "--interval", "0"])
    assert res.exit_code == 0, res.output
    assert len(calls) == 3
    assert "succeeded" in res.output


def test_wait_failed_exit_2(monkeypatch):
    _install(monkeypatch, _seq([{**TASK, "state": "running"}, {**TASK, "state": "failed", "error": "boom disk"}]))
    res = runner.invoke(cli.app, ["tasks", "wait", "7", "--interval", "0"])
    assert res.exit_code == 2
    assert "boom disk" in res.stderr


def test_wait_rolled_back_exit_2(monkeypatch):
    _install(monkeypatch, _seq([{**TASK, "state": "rolled_back", "error": "undone"}]))
    res = runner.invoke(cli.app, ["tasks", "wait", "7", "--interval", "0"])
    assert res.exit_code == 2


def test_wait_timeout_exit_4(monkeypatch):
    _install(monkeypatch, _seq([{**TASK, "state": "pending"}]))
    res = runner.invoke(cli.app, ["tasks", "wait", "7", "--timeout", "0", "--interval", "0"])
    assert res.exit_code == 4


def _mut_app():
    import typer

    app = typer.Typer()

    @app.callback()
    def main(ctx: typer.Context, json: bool = typer.Option(False, "--json")):
        ctx.obj = cli.Ctx(api="http://x", json=json)

    @app.command("poke")
    def poke(
        ctx: typer.Context,
        idempotency_key: str | None = cli.IDEMPOTENCY_OPT,
        wait: bool = cli.WAIT_OPT,
    ):
        cli.submit(ctx, "POST", "/things", {"a": 1}, idempotency_key, wait, timeout=5, interval=0)

    return app


def test_mutating_prints_task_id_and_key(monkeypatch):
    seen = []

    def h(r):
        seen.append((r.method, r.url.path, r.headers.get("Idempotency-Key"), json.loads(r.content)))
        return httpx.Response(202, json={"task_id": 42})

    _install(monkeypatch, h)
    res = runner.invoke(_mut_app(), ["poke", "--idempotency-key", "k1"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == "42"
    assert seen == [("POST", "/things", "k1", {"a": 1})]


def test_mutating_json_and_no_key(monkeypatch):
    seen = []

    def h(r):
        seen.append(r.headers.get("Idempotency-Key"))
        return httpx.Response(202, json={"task_id": 42})

    _install(monkeypatch, h)
    res = runner.invoke(_mut_app(), ["--json", "poke"])
    assert json.loads(res.stdout) == {"task_id": 42}
    assert seen == [None]


def test_mutating_wait(monkeypatch):
    def h(r):
        if r.method == "POST":
            return httpx.Response(202, json={"task_id": 42})
        return httpx.Response(200, json={**TASK, "id": 42, "state": "failed", "error": "bad"})

    _install(monkeypatch, h)
    res = runner.invoke(_mut_app(), ["poke", "--wait"])
    assert res.exit_code == 2
    assert "42" in res.stdout
    assert "bad" in res.stderr
