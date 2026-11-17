from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from minihci.driver.base import DomainSpec, DomainState, HostUnreachable
from minihci.models import HaRestart, Host, Task, Vm
from minihci.scheduler.config import load_config
from minihci.scheduler.placement import NoCapacity, place
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.locks import LOCK_NAMESPACE
from minihci.tasks.registry import register

# D-028: restart a VM from a dead host on a survivor, from the same disk. The worker already holds the
# per-VM advisory lock. start_domain has no short timeout: libvirt may block until the old host's NFS lease expires.


def _failed_at(ctx: StepContext) -> datetime:
    t = datetime.fromisoformat(ctx.params["failed_at"])
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _target(ctx: StepContext) -> tuple[Vm, str] | None:
    """The VM and the target host URI recorded by choose_host, or None if choose_host has not run."""
    with ctx.session_factory() as s:
        row = s.scalars(select(HaRestart).where(HaRestart.task_id == ctx.task_id)).one_or_none()
        if row is None:
            return None
        vm = s.get(Vm, ctx.target_id)
        s.expunge(vm)
        return vm, s.get(Host, row.to_host_id).libvirt_uri


def _assert_fenced(ctx: StepContext, vm_name: str) -> None:
    """Refuse unless the old host is still `dead` (read under the scheduler lock) and does not run the domain."""
    with ctx.session_factory() as s, s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        old = s.get(Host, ctx.params["dead_host_id"])
        name, status, uri = old.name, old.status, old.libvirt_uri
        reclaiming = s.scalars(
            select(Task.id)
            .where(Task.type == "host.reclaim", Task.target_id == old.id, Task.state.in_(("pending", "running")))
            .limit(1)
        ).first()
    if status != "dead":
        raise RuntimeError(f"host {name} is {status}, not dead; refusing to start a second copy of vm {vm_name}")
    if reclaiming is not None:
        raise RuntimeError(f"host {name} has an open host.reclaim; refusing to start a second copy of vm {vm_name}")
    try:
        if ctx.driver.domain_state(uri, vm_name) == DomainState.RUNNING:
            raise RuntimeError(f"vm {vm_name} is still running on host {name}; refusing to start a second copy")
    except HostUnreachable:
        pass


def choose_host(ctx: StepContext) -> None:
    with ctx.session_factory() as s:
        if s.scalars(select(HaRestart.id).where(HaRestart.task_id == ctx.task_id)).first() is not None:
            return  # resumed: keep the host already chosen
    with ctx.session_factory() as s, s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        old = s.get(Host, ctx.params["dead_host_id"])
        if old.status != "dead":
            raise RuntimeError(f"host {old.name} is {old.status}, not dead; refusing to restart its vm")
        vm = s.get(Vm, ctx.target_id)
        rows = s.execute(
            select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
            .where(Vm.desired_state != "absent", Vm.id != vm.id)
            .group_by(Vm.host_id)
        )
        usage = {hid: (int(c), int(m)) for hid, c, m in rows}
        # Other restarts already placed but not yet running still point at the dead host: count them on their target.
        inflight = s.execute(
            select(HaRestart.to_host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
            .join(Vm, Vm.id == HaRestart.vm_id)
            .join(Task, Task.id == HaRestart.task_id)
            .where(HaRestart.running_at.is_(None), Task.state.in_(("pending", "running")), Vm.id != vm.id)
            .group_by(HaRestart.to_host_id)
        )
        for hid, c, m in inflight:
            used_c, used_m = usage.get(hid, (0, 0))
            usage[hid] = (used_c + int(c), used_m + int(m))
        try:
            target = place(vm, list(s.scalars(select(Host).order_by(Host.id))), usage, load_config(s))
        except NoCapacity as e:
            raise RuntimeError(f"no surviving host has capacity for vm {vm.name}") from e
        s.execute(
            pg_insert(HaRestart)
            .values(
                task_id=ctx.task_id,
                vm_id=vm.id,
                from_host_id=old.id,
                to_host_id=target.id,
                failed_at=_failed_at(ctx),
            )
            .on_conflict_do_nothing(index_elements=["task_id"])
        )


def define_domain(ctx: StepContext) -> None:
    found = _target(ctx)
    if found is None:
        raise RuntimeError("choose_host has not recorded a target host")
    vm, uri = found
    if ctx.driver.domain_state(uri, vm.name) is None:
        ctx.driver.define_domain(
            uri, DomainSpec(name=vm.name, vcpus=vm.vcpus, mem_mb=vm.mem_mb, disk_path=vm.disk_path)
        )


def undo_define_domain(ctx: StepContext) -> None:
    found = _target(ctx)
    if found is None:
        return
    vm, uri = found
    state = ctx.driver.domain_state(uri, vm.name)
    if state == DomainState.RUNNING:
        ctx.driver.destroy_domain(uri, vm.name)
    if state is not None:
        ctx.driver.undefine_domain(uri, vm.name)


def start_domain(ctx: StepContext) -> None:
    found = _target(ctx)
    if found is None:
        raise RuntimeError("choose_host has not recorded a target host")
    vm, uri = found
    if ctx.driver.domain_state(uri, vm.name) != DomainState.RUNNING:
        _assert_fenced(ctx, vm.name)
        ctx.driver.start_domain(uri, vm.name)


def undo_start_domain(ctx: StepContext) -> None:
    found = _target(ctx)
    if found is None:
        return
    vm, uri = found
    if ctx.driver.domain_state(uri, vm.name) == DomainState.RUNNING:
        ctx.driver.destroy_domain(uri, vm.name)


def mark_running(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        row = s.scalars(select(HaRestart).where(HaRestart.task_id == ctx.task_id)).one()
        vm = s.get(Vm, ctx.target_id)
        vm.host_id = row.to_host_id
        vm.actual_state = "running"
        if row.running_at is None:
            now = datetime.now(UTC)
            row.running_at = now
            # the column is int32: clamp, or a long outage (>24 days) makes this step fail forever
            row.recovery_ms = min(2**31 - 1, max(0, int((now - row.failed_at).total_seconds() * 1000)))


def mark_pending_restart(ctx: StepContext) -> None:
    """Rolled back: if the old host is still dead and owns the VM, the next failover pass retries it."""
    dead_id = ctx.params["dead_host_id"]
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        old = s.get(Host, dead_id)
        if old.status == "dead" and vm.host_id == dead_id:
            vm.actual_state = "pending_restart"
        else:
            ctx.log.warning(
                "ha_restart_abandoned",
                extra={"vm_id": vm.id, "dead_host_id": dead_id, "host_status": old.status},
            )


VM_HA_RESTART = register(
    TaskType(
        "vm.ha_restart",
        "vm",
        [
            Step("choose_host", choose_host),
            Step("define_domain", define_domain, undo_define_domain),
            Step("start_domain", start_domain, undo_start_domain),
            Step("mark_running", mark_running),
        ],
        mark_pending_restart,
    )
)
