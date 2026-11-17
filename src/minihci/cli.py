"""MiniHCI CLI: a thin HTTP client over the REST API (D-029). Never touches the DB or drivers."""

import json as jsonlib
import time
from dataclasses import dataclass
from typing import Any

import httpx
import typer

DEFAULT_API = "http://localhost:8000"
EXIT_REJECTED = 1
EXIT_TASK_FAILED = 2
EXIT_UNREACHABLE = 3
EXIT_TIMEOUT = 4

app = typer.Typer(help="MiniHCI command line client", no_args_is_help=True)
tasks_app = typer.Typer(help="Inspect tasks", no_args_is_help=True)
app.add_typer(tasks_app, name="tasks")


@dataclass
class Ctx:
    api: str
    json: bool
    token: str | None = None


def make_client(api: str) -> httpx.Client:
    """Hook: tests monkeypatch this to inject a transport."""
    return httpx.Client(base_url=api, timeout=30.0)


@app.callback()
def main(
    ctx: typer.Context,
    api: str = typer.Option(DEFAULT_API, "--api", envvar="MINIHCI_API", help="API base URL"),
    json: bool = typer.Option(False, "--json", help="Print the API JSON unchanged"),
    token: str | None = typer.Option(None, "--token", envvar="MINIHCI_API_TOKEN", help="API bearer token"),
) -> None:
    ctx.obj = Ctx(api=api, json=json, token=token or None)


def _fail(msg: str, code: int) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(code)


def request(ctx: typer.Context, method: str, path: str, **kw: Any) -> Any:
    """Call the API and map errors to D-029 exit codes. Returns the decoded JSON body."""
    c: Ctx = ctx.obj
    try:
        if c.token:
            kw["headers"] = {**kw.get("headers", {}), "Authorization": f"Bearer {c.token}"}
        with make_client(c.api) as client:
            resp = client.request(method, path, **kw)
    except httpx.HTTPError as e:
        _fail(f"cannot reach API at {c.api}: {e}", EXIT_UNREACHABLE)
    if resp.status_code >= 500:
        _fail(f"API error {resp.status_code}: {resp.text}", EXIT_UNREACHABLE)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except (ValueError, AttributeError):
            detail = resp.text
        if not isinstance(detail, str):
            detail = jsonlib.dumps(detail)
        _fail(detail, EXIT_REJECTED)
    if not resp.content:
        return None
    return resp.json()


def print_table(rows: list[dict[str, Any]], cols: list[str]) -> None:
    cells = [[str(r.get(c, "")) if r.get(c) is not None else "" for c in cols] for r in rows]
    widths = [max([len(c)] + [len(row[i]) for row in cells]) for i, c in enumerate(cols)]
    typer.echo("  ".join(c.upper().ljust(w) for c, w in zip(cols, widths)).rstrip())
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths)).rstrip())


def print_obj(obj: dict[str, Any]) -> None:
    for k, v in obj.items():
        typer.echo(f"{k}: {jsonlib.dumps(v) if isinstance(v, (dict, list)) else v}")


def output(ctx: typer.Context, data: Any, cols: list[str] | None = None) -> None:
    if ctx.obj.json:
        typer.echo(jsonlib.dumps(data))
    elif isinstance(data, list):
        print_table(data, cols or (list(data[0]) if data else []))
    elif isinstance(data, dict):
        print_obj(data)


@tasks_app.command("list")
def tasks_list(ctx: typer.Context, state: str | None = typer.Option(None, "--state")) -> None:
    params = {"state": state} if state else None
    output(ctx, request(ctx, "GET", "/tasks", params=params), ["id", "type", "target_id", "state", "attempts"])


@tasks_app.command("show")
def tasks_show(ctx: typer.Context, task_id: int) -> None:
    output(ctx, request(ctx, "GET", f"/tasks/{task_id}"))


IDEMPOTENCY_OPT = typer.Option(None, "--idempotency-key", help="Sent as Idempotency-Key")
WAIT_OPT = typer.Option(False, "--wait", help="Wait for the task to finish")


def wait_for_task(ctx: typer.Context, task_id: int, timeout: float, interval: float) -> None:
    """Poll GET /tasks/{id}; exit 2 on failed/rolled_back, 4 on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        task = request(ctx, "GET", f"/tasks/{task_id}")
        state = task.get("state")
        if state == "succeeded":
            typer.echo(f"task {task_id}: succeeded")
            return
        if state in ("failed", "rolled_back"):
            _fail(f"task {task_id} {state}: {task.get('error')}", EXIT_TASK_FAILED)
        if time.monotonic() >= deadline:
            _fail(f"timed out waiting for task {task_id} (state {state})", EXIT_TIMEOUT)
        time.sleep(interval)


def submit(
    ctx: typer.Context,
    method: str,
    path: str,
    body: Any = None,
    idempotency_key: str | None = None,
    wait: bool = False,
    timeout: float = 600,
    interval: float = 1,
) -> None:
    """Shared path for mutating commands: send, print task_id (or 202 JSON), optionally wait."""
    kw: dict[str, Any] = {}
    if body is not None:
        kw["json"] = body
    if idempotency_key:
        kw["headers"] = {"Idempotency-Key": idempotency_key}
    data = request(ctx, method, path, **kw)
    if ctx.obj.json:
        typer.echo(jsonlib.dumps(data))
    else:
        typer.echo(str(data["task_id"]))
    if wait:
        wait_for_task(ctx, data["task_id"], timeout, interval)


@tasks_app.command("wait")
def tasks_wait(
    ctx: typer.Context,
    task_id: int,
    timeout: float = typer.Option(600, "--timeout"),
    interval: float = typer.Option(1, "--interval"),
) -> None:
    wait_for_task(ctx, task_id, timeout, interval)


hosts_app = typer.Typer(help="Manage hosts", no_args_is_help=True)
app.add_typer(hosts_app, name="hosts")


@hosts_app.command("add")
def hosts_add(
    ctx: typer.Context,
    name: str,
    uri: str,
    site: str = typer.Option("primary", "--site", help="primary or dr"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    body: dict[str, Any] = {"name": name, "libvirt_uri": uri}
    if site != "primary":
        body["site"] = site
    submit(ctx, "POST", "/hosts", body, idempotency_key, wait)


@hosts_app.command("list")
def hosts_list(ctx: typer.Context) -> None:
    output(ctx, request(ctx, "GET", "/hosts"), ["id", "name", "status"])


@hosts_app.command("rm")
def hosts_rm(
    ctx: typer.Context,
    host_id: int,
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    submit(ctx, "DELETE", f"/hosts/{host_id}", None, idempotency_key, wait)


@hosts_app.command("health")
def hosts_health(ctx: typer.Context, host_id: int) -> None:
    output(ctx, request(ctx, "GET", f"/hosts/{host_id}/health"))


vms_app = typer.Typer(help="Manage VMs", no_args_is_help=True)
app.add_typer(vms_app, name="vms")


@vms_app.command("create")
def vms_create(
    ctx: typer.Context,
    name: str,
    vcpus: int = typer.Option(..., "--vcpus"),
    mem_mb: int = typer.Option(..., "--mem-mb"),
    image: str = typer.Option(..., "--image"),
    host_id: int | None = typer.Option(None, "--host-id"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    body: dict[str, Any] = {"name": name, "vcpus": vcpus, "mem_mb": mem_mb, "image": image}
    if host_id is not None:
        body["host_id"] = host_id
    submit(ctx, "POST", "/vms", body, idempotency_key, wait)


@vms_app.command("list")
def vms_list(ctx: typer.Context) -> None:
    cols = ["id", "name", "host_id", "vcpus", "mem_mb", "desired_state", "actual_state"]
    output(ctx, request(ctx, "GET", "/vms"), cols)


@vms_app.command("show")
def vms_show(ctx: typer.Context, vm_id: int) -> None:
    output(ctx, request(ctx, "GET", f"/vms/{vm_id}"))


def _vm_action(ctx: typer.Context, vm_id: int, action: str, idempotency_key: str | None, wait: bool) -> None:
    submit(ctx, "POST", f"/vms/{vm_id}/{action}", None, idempotency_key, wait)


@vms_app.command("start")
def vms_start(
    ctx: typer.Context, vm_id: int, idempotency_key: str | None = IDEMPOTENCY_OPT, wait: bool = WAIT_OPT
) -> None:
    _vm_action(ctx, vm_id, "start", idempotency_key, wait)


@vms_app.command("stop")
def vms_stop(
    ctx: typer.Context,
    vm_id: int,
    force: bool = typer.Option(False, "--force", help="Hard power-off"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    submit(ctx, "POST", f"/vms/{vm_id}/stop", {"force": True} if force else None, idempotency_key, wait)


@vms_app.command("reboot")
def vms_reboot(
    ctx: typer.Context, vm_id: int, idempotency_key: str | None = IDEMPOTENCY_OPT, wait: bool = WAIT_OPT
) -> None:
    _vm_action(ctx, vm_id, "reboot", idempotency_key, wait)


@vms_app.command("migrate")
def vms_migrate(
    ctx: typer.Context,
    vm_id: int,
    to: int = typer.Option(..., "--to", help="Destination host id"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    submit(ctx, "POST", f"/vms/{vm_id}/migrate", {"host_id": to}, idempotency_key, wait)


@vms_app.command("rm")
def vms_rm(
    ctx: typer.Context, vm_id: int, idempotency_key: str | None = IDEMPOTENCY_OPT, wait: bool = WAIT_OPT
) -> None:
    submit(ctx, "DELETE", f"/vms/{vm_id}", None, idempotency_key, wait)


@vms_app.command("protect")
def vms_protect(
    ctx: typer.Context,
    vm_id: int,
    interval: int = typer.Option(..., "--interval", help="Replication interval in seconds"),
    retention: int = typer.Option(..., "--retention", help="Replicas to keep"),
    disable: bool = typer.Option(False, "--disable", help="Keep the policy but pause replication"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    body = {"interval_s": interval, "retention": retention, "enabled": not disable}
    submit(ctx, "PUT", f"/vms/{vm_id}/protection", body, idempotency_key, wait)


@vms_app.command("protection")
def vms_protection(ctx: typer.Context, vm_id: int) -> None:
    output(ctx, request(ctx, "GET", f"/vms/{vm_id}/protection"))


@vms_app.command("replicas")
def vms_replicas(ctx: typer.Context, vm_id: int) -> None:
    cols = ["id", "mode", "status", "seq", "snapshot_time", "size_bytes", "duration_ms"]
    output(ctx, request(ctx, "GET", f"/vms/{vm_id}/replicas"), cols)


@vms_app.command("failovers")
def vms_failovers(ctx: typer.Context, vm_id: int) -> None:
    """DR failovers of the VM, newest first; data_loss_s is a lower bound (D-037)."""
    cols = [
        "id",
        "task_id",
        "replica_id",
        "dr_host_id",
        "forced",
        "primary_failed_at",
        "running_at",
        "data_loss_s",
        "failover_ms",
    ]
    output(ctx, request(ctx, "GET", f"/vms/{vm_id}/failovers"), cols)


@vms_app.command("failover")
def vms_failover(
    ctx: typer.Context,
    vm_id: int,
    replica: int | None = typer.Option(None, "--replica", help="Replica id (default: newest succeeded)"),
    host: int | None = typer.Option(None, "--host", help="DR-site host id (default: any with capacity)"),
    force: bool = typer.Option(False, "--force", help="Fail over even if the source host is not dead"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    fields = {"replica_id": replica, "host_id": host, "force": True if force else None}
    body = {k: v for k, v in fields.items() if v is not None}
    submit(ctx, "POST", f"/vms/{vm_id}/failover", body or None, idempotency_key, wait)


@app.command("replication")
def replication(ctx: typer.Context) -> None:
    data = request(ctx, "GET", "/replication")
    if ctx.obj.json:
        output(ctx, data)
        return
    cols = ["vm_id", "vm_name", "interval_s", "last_replicated_at", "lag_s", "achieved_rpo_s", "within_target"]
    rows = [{c: "-" if r.get(c) is None else r[c] for c in cols} for r in data]
    print_table(rows, cols)


scheduler_app = typer.Typer(help="Scheduler configuration", no_args_is_help=True)
app.add_typer(scheduler_app, name="scheduler")


@scheduler_app.command("get")
def scheduler_get(ctx: typer.Context) -> None:
    output(ctx, request(ctx, "GET", "/scheduler/config"))


@scheduler_app.command("set")
def scheduler_set(
    ctx: typer.Context,
    cpu_headroom_pct: int | None = typer.Option(None, "--cpu-headroom-pct"),
    rebalance_enabled: bool | None = typer.Option(None, "--rebalance-enabled/--no-rebalance-enabled"),
    rebalance_cpu_threshold_pct: int | None = typer.Option(None, "--rebalance-cpu-threshold-pct"),
    rebalance_window_s: int | None = typer.Option(None, "--rebalance-window-s"),
    rebalance_vm_cooldown_s: int | None = typer.Option(None, "--rebalance-vm-cooldown-s"),
    rebalance_host_cooldown_s: int | None = typer.Option(None, "--rebalance-host-cooldown-s"),
    idempotency_key: str | None = IDEMPOTENCY_OPT,
    wait: bool = WAIT_OPT,
) -> None:
    fields = {
        "cpu_headroom_pct": cpu_headroom_pct,
        "rebalance_enabled": rebalance_enabled,
        "rebalance_cpu_threshold_pct": rebalance_cpu_threshold_pct,
        "rebalance_window_s": rebalance_window_s,
        "rebalance_vm_cooldown_s": rebalance_vm_cooldown_s,
        "rebalance_host_cooldown_s": rebalance_host_cooldown_s,
    }
    body = {k: v for k, v in fields.items() if v is not None}
    if not body:
        _fail("no scheduler settings given", EXIT_REJECTED)
    submit(ctx, "PUT", "/scheduler/config", body, idempotency_key, wait)
