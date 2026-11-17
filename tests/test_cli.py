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


def test_hosts_add_with_key(monkeypatch):
    seen = []

    def h(r):
        seen.append((r.method, r.url.path, r.headers.get("Idempotency-Key"), json.loads(r.content)))
        return httpx.Response(202, json={"task_id": 5})

    _install(monkeypatch, h)
    res = runner.invoke(cli.app, ["hosts", "add", "h1", "qemu+ssh://h1/system", "--idempotency-key", "k"])
    assert res.exit_code == 0, res.output
    assert res.stdout.strip() == "5"
    assert seen == [("POST", "/hosts", "k", {"name": "h1", "libvirt_uri": "qemu+ssh://h1/system"})]


def test_hosts_add_conflict(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(409, json={"detail": "host exists"}))
    res = runner.invoke(cli.app, ["hosts", "add", "h1", "u"])
    assert res.exit_code == 1
    assert "host exists" in res.stderr


def test_hosts_list_table(monkeypatch):
    _install(monkeypatch, lambda r: httpx.Response(200, json=[{"id": 3, "name": "h3", "status": "active"}]))
    res = runner.invoke(cli.app, ["hosts", "list"])
    assert res.exit_code == 0, res.output
    lines = res.stdout.splitlines()
    assert lines[0].split() == ["ID", "NAME", "STATUS"]
    assert lines[1].split() == ["3", "h3", "active"]


def test_hosts_rm(monkeypatch):
    seen = []

    def h(r):
        seen.append((r.method, r.url.path))
        return httpx.Response(202, json={"task_id": 9})

    _install(monkeypatch, h)
    res = runner.invoke(cli.app, ["hosts", "rm", "3"])
    assert res.exit_code == 0, res.output
    assert seen == [("DELETE", "/hosts/3")]


def test_hosts_health(monkeypatch):
    seen = []

    def h(r):
        seen.append((r.method, r.url.path))
        return httpx.Response(200, json={"host_id": 3, "healthy": True})

    _install(monkeypatch, h)
    res = runner.invoke(cli.app, ["hosts", "health", "3"])
    assert res.exit_code == 0, res.output
    assert seen == [("GET", "/hosts/3/health")]
    assert "healthy: True" in res.stdout


def _capture(monkeypatch, status=202, body=None):
    reqs = []

    def h(r):
        reqs.append((r.method, r.url.path, json.loads(r.content) if r.content else None))
        return httpx.Response(status, json=body if body is not None else {"task_id": 5, "target_id": 1})

    _install(monkeypatch, h)
    return reqs


def test_vms_create(monkeypatch):
    reqs = _capture(monkeypatch)
    res = runner.invoke(
        cli.app, ["vms", "create", "web", "--vcpus", "2", "--mem-mb", "512", "--image", "cirros", "--host-id", "3"]
    )
    assert res.exit_code == 0, res.output
    assert reqs == [("POST", "/vms", {"name": "web", "vcpus": 2, "mem_mb": 512, "image": "cirros", "host_id": 3})]
    assert res.stdout.strip() == "5"


def test_vms_create_no_host(monkeypatch):
    reqs = _capture(monkeypatch)
    res = runner.invoke(cli.app, ["vms", "create", "web", "--vcpus", "1", "--mem-mb", "64", "--image", "c"])
    assert res.exit_code == 0
    assert "host_id" not in reqs[0][2]


def test_vms_create_422(monkeypatch):
    _capture(monkeypatch, 422, {"detail": [{"msg": "bad"}]})
    res = runner.invoke(cli.app, ["vms", "create", "W", "--vcpus", "1", "--mem-mb", "64", "--image", "c"])
    assert res.exit_code == 1


def test_vms_list_show(monkeypatch):
    reqs = _capture(monkeypatch, 200, [{"id": 1, "name": "a", "host_id": 2, "actual_state": "running"}])
    res = runner.invoke(cli.app, ["vms", "list"])
    assert res.exit_code == 0 and "running" in res.output
    assert reqs[0][:2] == ("GET", "/vms")
    reqs.clear()
    runner.invoke(cli.app, ["vms", "show", "1"])
    assert reqs[0][:2] == ("GET", "/vms/1")


def test_vms_actions(monkeypatch):
    reqs = _capture(monkeypatch)
    for args in (["start", "1"], ["reboot", "1"], ["stop", "1"], ["stop", "1", "--force"], ["rm", "1"]):
        assert runner.invoke(cli.app, ["vms", *args]).exit_code == 0
    assert reqs == [
        ("POST", "/vms/1/start", None),
        ("POST", "/vms/1/reboot", None),
        ("POST", "/vms/1/stop", None),
        ("POST", "/vms/1/stop", {"force": True}),
        ("DELETE", "/vms/1", None),
    ]


def test_vms_migrate(monkeypatch):
    reqs = _capture(monkeypatch)
    res = runner.invoke(cli.app, ["vms", "migrate", "1", "--to", "2", "--idempotency-key", "k"])
    assert res.exit_code == 0
    assert reqs == [("POST", "/vms/1/migrate", {"host_id": 2})]


def test_scheduler_get(monkeypatch):
    reqs = _capture(monkeypatch, 200, {"cpu_headroom_pct": 20})
    res = runner.invoke(cli.app, ["scheduler", "get"])
    assert res.exit_code == 0 and "cpu_headroom_pct: 20" in res.output
    assert reqs[0][:2] == ("GET", "/scheduler/config")


def test_scheduler_set(monkeypatch):
    reqs = _capture(monkeypatch)
    res = runner.invoke(cli.app, ["scheduler", "set", "--cpu-headroom-pct", "30", "--no-rebalance-enabled"])
    assert res.exit_code == 0, res.output
    assert reqs == [("PUT", "/scheduler/config", {"cpu_headroom_pct": 30, "rebalance_enabled": False})]


def test_scheduler_set_nothing(monkeypatch):
    reqs = _capture(monkeypatch)
    res = runner.invoke(cli.app, ["scheduler", "set"])
    assert res.exit_code == 1
    assert reqs == []


def test_end_to_end_against_api(monkeypatch, session_factory):
    from fastapi.testclient import TestClient

    from minihci.app import create_app
    from minihci.config import Settings
    from minihci.driver.fake import FakeDriver
    from tests.helpers import drain

    uri = "qemu+ssh://a/system"
    drv = FakeDriver()
    drv.add_host(uri, 8, 16384)
    api = TestClient(create_app(Settings(), session_factory=session_factory))
    monkeypatch.setattr(cli, "make_client", lambda base: api)

    def run(*args):
        res = runner.invoke(cli.app, list(args))
        assert res.exit_code == 0, res.output
        return res

    res = run("--json", "hosts", "add", "a", uri)
    host_task = json.loads(res.stdout)["task_id"]
    drain(session_factory, drv)
    run("tasks", "wait", str(host_task), "--interval", "0")

    run("vms", "create", "web1", "--vcpus", "1", "--mem-mb", "512", "--image", "/srv/minihci/base/alpine.qcow2")
    drain(session_factory, drv)
    vms = json.loads(run("--json", "vms", "list").stdout)
    assert [(v["name"], v["actual_state"]) for v in vms] == [("web1", "running")]


def test_console_script_help():
    import subprocess

    res = subprocess.run(["uv", "run", "minihci", "--help"], check=False, capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert "hosts" in res.stdout
