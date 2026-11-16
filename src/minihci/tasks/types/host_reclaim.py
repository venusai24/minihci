from sqlalchemy import select, text

from minihci.driver.base import DomainState
from minihci.models import Host, Vm
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.locks import LOCK_NAMESPACE
from minihci.tasks.registry import register

# D-028 (5): a dead host that answers again stays `dead` until its stale copies are gone.
# Forward-only and idempotent: each step looks at the real state first. The worker holds the host lock.


def _stale(ctx: StepContext, name: str) -> bool:
    """Stale unless a live VM row with this name points at this host."""
    with ctx.session_factory() as s:
        rows = list(s.scalars(select(Vm).where(Vm.name == name)))
    return not any(v.desired_state != "absent" and v.host_id == ctx.target_id for v in rows)


def remove_stale(ctx: StepContext) -> None:
    with ctx.session_factory() as s:
        uri = s.get(Host, ctx.target_id).libvirt_uri
    for name in sorted(ctx.driver.list_domains(uri)):
        removed = None
        # Lock every VM row with this name (ascending id) first, and only then decide, under the lock.
        with ctx.session_factory() as s, s.begin():
            for vm_id in s.scalars(select(Vm.id).where(Vm.name == name).order_by(Vm.id)).all():
                s.execute(text("SELECT pg_advisory_xact_lock(:ns, :id)"), {"ns": LOCK_NAMESPACE["vm"], "id": vm_id})
            if not _stale(ctx, name):
                continue
            state = ctx.driver.domain_state(uri, name)
            if state is None:
                continue
            if state == DomainState.RUNNING:
                ctx.driver.destroy_domain(uri, name)
            ctx.driver.undefine_domain(uri, name)
            removed = state
        ctx.log.warning(
            "stale copy removed",
            extra={
                "fields": {"event": "stale_copy_removed", "vm": name, "host_id": ctx.target_id, "was": str(removed)}
            },
        )


def mark_ready(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        host = s.get(Host, ctx.target_id)
        if host.status == "dead" and host.missed_heartbeats == 0:
            host.status = "ready"


HOST_RECLAIM = register(
    TaskType(
        name="host.reclaim",
        target_kind="host",
        steps=[Step("remove_stale", remove_stale), Step("mark_ready", mark_ready)],
    )
)
