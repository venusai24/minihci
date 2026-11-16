from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from minihci.driver.base import DomainState
from minihci.models import Host, Migration, Vm
from minihci.scheduler.config import load_config
from minihci.scheduler.placement import PLACEABLE_STATUSES, fits
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.locks import LOCK_NAMESPACE
from minihci.tasks.registry import register

# Forward-only (D-024): no step has an undo. Every step looks at where the domain really is first.
# The worker already holds the per-VM advisory lock; check_destination also takes the scheduler lock (3, 0).


def _hosts(ctx: StepContext) -> tuple[str, str, str]:
    """Return (vm name, src uri, dst uri)."""
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        return (
            vm.name,
            s.get(Host, ctx.params["src_host_id"]).libvirt_uri,
            s.get(Host, ctx.params["dst_host_id"]).libvirt_uri,
        )


def check_destination(ctx: StepContext) -> None:
    name, _src, dst = _hosts(ctx)
    on_dst = ctx.driver.domain_state(dst, name) is not None
    with ctx.session_factory() as s, s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        if not on_dst:
            vm = s.get(Vm, ctx.target_id)
            host = s.get(Host, ctx.params["dst_host_id"])
            if host.status not in PLACEABLE_STATUSES:
                raise RuntimeError(f"destination host {host.name} is {host.status}, not placeable")
            rows = s.execute(
                select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
                .where(Vm.desired_state != "absent", Vm.id != vm.id)
                .group_by(Vm.host_id)
            )
            usage = {hid: (int(c), int(m)) for hid, c, m in rows}
            if not fits(vm, host, usage, load_config(s)):
                raise RuntimeError(f"destination host {host.name} has no capacity for this vm")
        s.execute(
            pg_insert(Migration)
            .values(
                task_id=ctx.task_id,
                vm_id=ctx.target_id,
                src_host_id=ctx.params["src_host_id"],
                dst_host_id=ctx.params["dst_host_id"],
            )
            .on_conflict_do_nothing(index_elements=["task_id"])
        )


def migrate(ctx: StepContext) -> None:
    name, src, dst = _hosts(ctx)
    src_state = ctx.driver.domain_state(src, name)
    dst_state = ctx.driver.domain_state(dst, name)
    if src_state == DomainState.RUNNING and dst_state is None:
        result = ctx.driver.migrate_live(src, dst, name)
        with ctx.session_factory() as s, s.begin():
            row = s.scalars(select(Migration).where(Migration.task_id == ctx.task_id)).one()
            row.duration_ms, row.downtime_ms = result.duration_ms, result.downtime_ms
    elif src_state is None and dst_state == DomainState.RUNNING:
        ctx.log.info("migration_unmeasured: domain %s already on destination", name)
    elif src_state is not None and src_state != DomainState.RUNNING:
        raise RuntimeError(f"vm {name} is not running on the source host ({src_state.value})")
    else:
        raise RuntimeError(f"unexpected domain placement for {name}: src={src_state}, dst={dst_state}")


def switch_host(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        s.get(Vm, ctx.target_id).host_id = ctx.params["dst_host_id"]


def finish(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        row = s.scalars(select(Migration).where(Migration.task_id == ctx.task_id)).one()
        row.status = "succeeded"
        row.finished_at = func.now()


def sync_host(ctx: StepContext) -> None:
    """Point vms.host_id at the host that really has the domain; close the running migrations row (succeeded only if the domain is on dst)."""
    name, src, dst = _hosts(ctx)
    on_src = ctx.driver.domain_state(src, name) is not None
    on_dst = ctx.driver.domain_state(dst, name) is not None
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        real, outcome = vm.host_id, "failed"
        if on_dst and not on_src:
            real, outcome = ctx.params["dst_host_id"], "succeeded"
        elif on_src and not on_dst:
            real = ctx.params["src_host_id"]
        else:
            # On both hosts or on neither: do not guess a host, but never leave the row running.
            ctx.log.warning(
                "migration placement ambiguous",
                extra={
                    "fields": {
                        "event": "migrate_placement_ambiguous",
                        "task_id": ctx.task_id,
                        "vm_id": ctx.target_id,
                        "on_src": on_src,
                        "on_dst": on_dst,
                    }
                },
            )
        if vm.host_id != real:
            ctx.log.warning(
                "migration host drift",
                extra={
                    "fields": {
                        "event": "migrate_host_drift",
                        "task_id": ctx.task_id,
                        "vm_id": ctx.target_id,
                        "expected": vm.host_id,
                        "observed": real,
                    }
                },
            )
            vm.host_id = real
        row = s.scalars(select(Migration).where(Migration.task_id == ctx.task_id)).one_or_none()
        if row is not None and row.status == "running":
            row.status = outcome
            row.finished_at = func.now()


VM_MIGRATE = register(
    TaskType(
        "vm.migrate",
        "vm",
        [
            Step("check_destination", check_destination),
            Step("migrate", migrate),
            Step("switch_host", switch_host),
            Step("finish", finish),
        ],
        sync_host,
    )
)
