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


def make_client(api: str) -> httpx.Client:
    """Hook: tests monkeypatch this to inject a transport."""
    return httpx.Client(base_url=api, timeout=30.0)


@app.callback()
def main(
    ctx: typer.Context,
    api: str = typer.Option(DEFAULT_API, "--api", envvar="MINIHCI_API", help="API base URL"),
    json: bool = typer.Option(False, "--json", help="Print the API JSON unchanged"),
) -> None:
    ctx.obj = Ctx(api=api, json=json)


def _fail(msg: str, code: int) -> None:
    typer.echo(msg, err=True)
    raise typer.Exit(code)


def request(ctx: typer.Context, method: str, path: str, **kw: Any) -> Any:
    """Call the API and map errors to D-029 exit codes. Returns the decoded JSON body."""
    c: Ctx = ctx.obj
    try:
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
