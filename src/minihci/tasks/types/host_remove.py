from sqlalchemy import select

from minihci.models import Host, Vm
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register


def check_empty(ctx: StepContext) -> None:
    """Read-only, so trivially safe to retry. Backstop for the API's up-front check (D-016)."""
    with ctx.session_factory() as s:
        names = list(
            s.scalars(
                select(Vm.name).where(Vm.host_id == ctx.target_id, Vm.desired_state != "absent").order_by(Vm.name)
            )
        )
    if names:
        raise RuntimeError(f"host has {len(names)} VM(s): {', '.join(names)}")


def mark_removed(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        host = s.get(Host, ctx.target_id)
        if host.status != "removed":
            host.status = "removed"


def undo_mark_removed(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        host = s.get(Host, ctx.target_id)
        if host.status == "removed":
            host.status = "removing"


def restore_status(ctx: StepContext) -> None:
    prev = ctx.params.get("prev_status")
    if prev is None:
        raise RuntimeError("host.remove: params.prev_status missing; host left in 'removing'")
    with ctx.session_factory() as s, s.begin():
        host = s.get(Host, ctx.target_id)
        if host.status == "removing":
            host.status = prev


HOST_REMOVE = register(
    TaskType(
        name="host.remove",
        target_kind="host",
        steps=[Step("check_empty", check_empty), Step("mark_removed", mark_removed, undo=undo_mark_removed)],
        on_rolled_back=restore_status,
    )
)
