from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from minihci.driver.base import DomainSpec, DomainState, HostUnreachable
from minihci.models import DrFailover, Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.locks import LOCK_NAMESPACE
from minihci.tasks.registry import register

# D-037: boot a fresh overlay on a replica at the DR site, fenced like HA (D-028). The worker already holds the
# per-VM advisory lock. Params (resolved by the API, so a replay is deterministic): replica_id, dr_host_id, force.
# The dr_failovers row (step `record`) is the record of intent; its running_at is set in the same transaction as the
# switch of the VM row, so it also marks "the DR copy is now the VM": after that no undo may touch the DR copy.


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _row(ctx: StepContext) -> DrFailover | None:
    with ctx.session_factory() as s:
        row = s.scalars(select(DrFailover).where(DrFailover.task_id == ctx.task_id)).one_or_none()
        if row is not None:
            s.expunge(row)
        return row


def _need_row(ctx: StepContext) -> DrFailover:
    row = _row(ctx)
    if row is None:
        raise RuntimeError("record has not written a dr_failovers row")
    return row


def _vm(ctx: StepContext) -> Vm:
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        s.expunge(vm)
        return vm


def _uri(ctx: StepContext, host_id: int) -> str:
    with ctx.session_factory() as s:
        return s.get(Host, host_id).libvirt_uri


def _refuse_if_switched(row: DrFailover | None, vm_id: int) -> None:
    """An undo that reaches a committed switch (e.g. a recovery rollback after a crash right after `switch`) must not
    destroy the DR copy or delete its disk: the VM row already points at them and the guest may have written to it."""
    if row is not None and row.running_at is not None:
        raise RuntimeError(f"failover already switched vm {vm_id} to the DR copy; leaving the DR copy in place")


def record(ctx: StepContext) -> None:
    if _row(ctx) is not None:
        return
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        src = s.get(Host, vm.host_id)
        replica = s.get(Replica, ctx.params["replica_id"])
        dr = s.get(Host, ctx.params["dr_host_id"])
        if src.site == "dr":
            raise RuntimeError(f"vm {vm.name} already runs at the DR site")
        if replica is None or replica.vm_id != vm.id or replica.status != "succeeded":
            raise RuntimeError(f"replica {ctx.params['replica_id']} is not a succeeded replica of vm {vm.name}")
        if dr is None or dr.site != "dr":
            raise RuntimeError(f"host {ctx.params['dr_host_id']} is not a DR-site host")
        failed_at = _aware(src.last_heartbeat) if src.status == "dead" and src.last_heartbeat is not None else None
        s.execute(
            pg_insert(DrFailover)
            .values(
                task_id=ctx.task_id,
                vm_id=vm.id,
                replica_id=replica.id,
                from_host_id=src.id,
                dr_host_id=dr.id,
                from_disk_path=vm.disk_path,
                disk_path=f"{ctx.settings.dr_root}/vm{vm.id}-failover-t{ctx.task_id}.qcow2",
                forced=bool(ctx.params["force"]),
                primary_failed_at=failed_at,
                started_at=_now(),
            )
            .on_conflict_do_nothing(index_elements=["task_id"])
        )


def _check_dead(ctx: StepContext, row: DrFailover, vm_name: str) -> tuple[str, str]:
    """Unless forced, refuse unless the old host is still `dead` with no open host.reclaim (read under the scheduler
    lock (3, 0), as D-028). Returns the old host's name and URI."""
    with ctx.session_factory() as s, s.begin():
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        old = s.get(Host, row.from_host_id)
        name, status, uri = old.name, old.status, old.libvirt_uri
        reclaiming = s.scalars(
            select(Task.id)
            .where(Task.type == "host.reclaim", Task.target_id == old.id, Task.state.in_(("pending", "running")))
            .limit(1)
        ).first()
    if not row.forced:
        if status != "dead":
            raise RuntimeError(f"host {name} is {status}, not dead; refusing to start a second copy of vm {vm_name}")
        if reclaiming is not None:
            raise RuntimeError(f"host {name} has an open host.reclaim; refusing to start a second copy of vm {vm_name}")
    return name, uri


def fence_primary(ctx: StepContext) -> None:
    row = _need_row(ctx)
    if row.running_at is not None:
        return  # switched: the DR copy is the VM now
    vm = _vm(ctx)
    name, uri = _check_dead(ctx, row, vm.name)
    try:
        state = ctx.driver.domain_state(uri, vm.name)
    except HostUnreachable:
        return  # fenced
    if state is None or not state.active:
        return
    if not row.forced:  # running, paused or crashed: it could still run again
        raise RuntimeError(f"vm {vm.name} is still {state.value} on host {name}; refusing to start a second copy")
    ctx.driver.destroy_domain(uri, vm.name)
    ctx.log.warning(
        "dr_failover_primary_destroyed",
        extra={"fields": {"event": "dr_failover_primary_destroyed", "vm_id": vm.id, "host_id": row.from_host_id}},
    )


def prepare_disk(ctx: StepContext) -> None:
    row = _need_row(ctx)
    if row.running_at is not None:
        return
    with ctx.session_factory() as s:
        replica = s.get(Replica, row.replica_id)
        base, status = replica.dr_path, replica.status
    if status != "succeeded" or not base or not ctx.driver.disk_exists(base):
        raise RuntimeError(f"replica {row.replica_id} ({status}) file {base} is missing; refusing to fail over")
    if not ctx.driver.disk_exists(row.disk_path):
        ctx.driver.create_disk_overlay(base, row.disk_path)


def undo_prepare_disk(ctx: StepContext) -> None:
    row = _row(ctx)
    if row is None:
        return
    _refuse_if_switched(row, ctx.target_id)
    if ctx.driver.disk_exists(row.disk_path):  # named after this task: only ever ours
        ctx.driver.delete_disk(row.disk_path)


def define_domain(ctx: StepContext) -> None:
    row = _need_row(ctx)
    if row.running_at is not None:
        return
    vm = _vm(ctx)
    uri = _uri(ctx, row.dr_host_id)
    state = ctx.driver.domain_state(uri, vm.name)
    # Steps run in order and a done step is skipped, so this step never runs after this task's start_domain (and
    # after the switch it returned above): an active domain of that name is never ours. Refuse, touching nothing.
    if state is not None and state.active:
        raise RuntimeError(f"a domain named {vm.name} is already {state.value} on the DR host; refusing")
    if state is not None:
        # Shut off: a leftover (an earlier attempt, ours from a crashed run of this step, or a stale copy). HostDriver
        # cannot tell which disk it uses, so never trust it: replace it with this task's spec. A crash between the
        # undefine and the define leaves no domain, and the rerun just defines.
        ctx.driver.undefine_domain(uri, vm.name)
    ctx.driver.define_domain(uri, DomainSpec(name=vm.name, vcpus=vm.vcpus, mem_mb=vm.mem_mb, disk_path=row.disk_path))


def _step_ran(ctx: StepContext, name: str) -> bool:
    """True if this task's step `name` has started at least once (the step log is recorded before each run)."""
    with ctx.session_factory() as s:
        steps = s.get(Task, ctx.task_id).steps or []
    return any(st["name"] == name and st["status"] != "pending" for st in steps)


def undo_define_domain(ctx: StepContext) -> None:
    row = _row(ctx)
    if row is None:
        return
    _refuse_if_switched(row, ctx.target_id)
    vm = _vm(ctx)
    uri = _uri(ctx, row.dr_host_id)
    state = ctx.driver.domain_state(uri, vm.name)
    if state is None:
        return
    if state.active:
        # Only this task's start_domain can have made our domain active. If it never ran, the active domain is the
        # foreign one define_domain refused: leave it alone.
        if not _step_ran(ctx, "start_domain"):
            ctx.log.warning(
                "dr_failover_foreign_domain_kept",
                extra={"fields": {"event": "dr_failover_foreign_domain_kept", "vm_id": vm.id, "domain": vm.name}},
            )
            return
        ctx.driver.destroy_domain(uri, vm.name)
    ctx.driver.undefine_domain(uri, vm.name)


def start_domain(ctx: StepContext) -> None:
    row = _need_row(ctx)
    vm = _vm(ctx)
    uri = _uri(ctx, row.dr_host_id)
    if ctx.driver.domain_state(uri, vm.name) == DomainState.RUNNING:
        return
    # fence_primary may have run in an earlier attempt: re-check the DB fence under (3, 0) right before the start
    _check_dead(ctx, row, vm.name)
    ctx.driver.start_domain(uri, vm.name)


def undo_start_domain(ctx: StepContext) -> None:
    row = _row(ctx)
    if row is None:
        return
    _refuse_if_switched(row, ctx.target_id)
    vm = _vm(ctx)
    uri = _uri(ctx, row.dr_host_id)
    state = ctx.driver.domain_state(uri, vm.name)
    if state is not None and state.active:
        ctx.driver.destroy_domain(uri, vm.name)


def switch(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        row = s.scalars(select(DrFailover).where(DrFailover.task_id == ctx.task_id).with_for_update()).one()
        vm = s.get(Vm, ctx.target_id)
        vm.host_id = row.dr_host_id
        vm.disk_path = row.disk_path
        vm.desired_state = "running"
        vm.actual_state = "running"
        vm.state_reason = None
        policy = s.get(ProtectionPolicy, vm.id)
        if policy is not None:
            policy.enabled = False
        if row.running_at is None:
            now = _now()
            row.running_at = now
            # the column is int32: clamp, as vm_ha_restart.mark_running does
            ms = int((now - _aware(row.started_at)).total_seconds() * 1000)
            row.failover_ms = min(2**31 - 1, max(0, ms))
            replica = s.get(Replica, row.replica_id)
            if row.primary_failed_at is not None and replica.snapshot_time is not None:
                row.data_loss_s = (_aware(row.primary_failed_at) - _aware(replica.snapshot_time)).total_seconds()
        fields = {
            "event": "dr_failover_running",
            "vm_id": vm.id,
            "replica_id": row.replica_id,
            "dr_host_id": row.dr_host_id,
            "forced": row.forced,
            "failover_ms": row.failover_ms,
            "data_loss_s": row.data_loss_s,
        }
    ctx.log.info("dr_failover_running", extra={"fields": fields})


def log_rolled_back(ctx: StepContext) -> None:
    """The DB is left as it was (apart from the step log): the VM still points at its primary host and disk."""
    ctx.log.warning(
        "dr_failover_rolled_back",
        extra={
            "fields": {
                "event": "dr_failover_rolled_back",
                "vm_id": ctx.target_id,
                "replica_id": ctx.params.get("replica_id"),
            }
        },
    )


VM_DR_FAILOVER = register(
    TaskType(
        "vm.dr_failover",
        "vm",
        [
            Step("record", record),
            Step("fence_primary", fence_primary),
            Step("prepare_disk", prepare_disk, undo_prepare_disk),
            Step("define_domain", define_domain, undo_define_domain),
            Step("start_domain", start_domain, undo_start_domain),
            Step("switch", switch),
        ],
        log_rolled_back,
    )
)
