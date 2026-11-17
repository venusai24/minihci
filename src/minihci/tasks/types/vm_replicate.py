import time
from datetime import UTC, datetime

from sqlalchemy import select

from minihci.dr.chain import ReplicaInfo, next_replica, prune_ids
from minihci.driver.base import CheckpointNotFound, DomainNotFound, DriverError, HostDriver, HostUnreachable
from minihci.models import Host, ProtectionPolicy, Replica, Task, Vm
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register

# D-034/D-035: one task = one replica (a full copy or an incremental overlay on the previous one).
# The worker already holds the per-VM advisory lock. Every step checks the real state first; the replicas row is the
# record of intent, written before any file or checkpoint exists. MiniHCI only ever touches checkpoints named
# `minihci-*`: the libvirt driver does not guard names, so the guard lives here.

CHECKPOINT_PREFIX = "minihci-"
ABORT_POLLS = 60
ABORT_POLL_S = 0.5
_sleep = time.sleep  # tests replace it so the wait does not run in real time


def stop_backup_job(driver: HostDriver, uri: str, name: str) -> None:
    """Abort a running backup job and wait until it is really gone: on a real host abortJob() returns while the job
    is still unwinding, and its checkpoint can only be dropped (or a new backup started) afterwards."""
    if not driver.backup_active(uri, name):
        return
    try:
        driver.abort_backup(uri, name)
    except DriverError:
        pass  # the job ended between the check and the abort ("no backup job"); the poll below decides
    for _ in range(ABORT_POLLS):
        if not driver.backup_active(uri, name):
            return
        _sleep(ABORT_POLL_S)
    raise RuntimeError(f"backup job on {name} is still active after abort")


def delete_minihci_checkpoints(driver: HostDriver, uri: str, name: str, keep: str | None = None) -> None:
    """Delete every `minihci-*` checkpoint of the domain except `keep`. Foreign checkpoints are never touched."""
    for cp in driver.list_checkpoints(uri, name):
        if cp.startswith(CHECKPOINT_PREFIX) and cp != keep:
            try:
                driver.delete_checkpoint(uri, name, cp)
            except CheckpointNotFound:
                pass


def _row(ctx: StepContext) -> Replica | None:
    with ctx.session_factory() as s:
        row = s.scalars(select(Replica).where(Replica.task_id == ctx.task_id)).one_or_none()
        if row is not None:
            s.expunge(row)
        return row


def _need_row(ctx: StepContext) -> Replica:
    row = _row(ctx)
    if row is None:
        raise RuntimeError("plan has not recorded a replica")
    return row


def _uri(ctx: StepContext, host_id: int) -> str:
    with ctx.session_factory() as s:
        return s.get(Host, host_id).libvirt_uri


def _vm(ctx: StepContext) -> Vm:
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        s.expunge(vm)
        return vm


def _info(r: Replica) -> ReplicaInfo:
    return ReplicaInfo(
        id=r.id,
        chain_id=r.chain_id,
        seq=r.seq,
        dr_path=r.dr_path,
        checkpoint=r.checkpoint,
        snapshot_time=r.snapshot_time,
        finished_at=r.finished_at,
    )


def _succeeded(s, vm_id: int) -> list[Replica]:
    return list(s.scalars(select(Replica).where(Replica.vm_id == vm_id, Replica.status == "succeeded")))


def plan(ctx: StepContext) -> None:
    if _row(ctx) is not None:
        return
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        host = s.get(Host, vm.host_id)
        policy = s.get(ProtectionPolicy, vm.id)
        if vm.desired_state != "running" or vm.actual_state != "running":
            raise RuntimeError(f"vm {vm.name} is {vm.desired_state}/{vm.actual_state}, not running")
        if host.status != "ready" or host.site != "primary":
            raise RuntimeError(f"host {host.name} is {host.status} on site {host.site}, not a ready primary host")
        if policy is None or not policy.enabled:
            raise RuntimeError(f"vm {vm.name} has no enabled protection policy")
        name, host_id, uri, retention = vm.name, host.id, host.libvirt_uri, policy.retention
        succeeded = _succeeded(s, vm.id)
        infos = [_info(r) for r in succeeded]
        last = max(succeeded, key=lambda r: (r.snapshot_time, r.id)) if succeeded else None
        last_path = last.dr_path if last else None
        # the newest finished attempt (this task's own row does not exist yet)
        newest = s.scalars(
            select(Replica)
            .where(Replica.vm_id == vm.id, Replica.status.in_(("succeeded", "failed", "pruned")))
            .order_by(Replica.id.desc())
            .limit(1)
        ).first()
        failed_incremental = newest is not None and newest.status == "failed" and newest.mode == "incremental"
    checkpoints = ctx.driver.list_checkpoints(uri, name)
    present = ctx.driver.disk_exists(last_path) if last_path else False
    nxt = next_replica(infos, retention, checkpoints, present, failed_incremental)
    with ctx.session_factory() as s, s.begin():
        if s.scalars(select(Replica.id).where(Replica.task_id == ctx.task_id)).first() is not None:
            return
        row = Replica(
            vm_id=ctx.target_id,
            task_id=ctx.task_id,
            host_id=host_id,
            mode=nxt.mode,
            status="running",
            seq=nxt.seq,
            since_checkpoint=nxt.since,
            backing_path=nxt.backing,
        )
        s.add(row)
        s.flush()
        row.dr_path = f"{ctx.settings.dr_root}/vm{ctx.target_id}-r{row.id}.qcow2"
        row.checkpoint = f"{CHECKPOINT_PREFIX}r{row.id}"
        row.chain_id = row.id if nxt.mode == "full" else nxt.chain_id
    if nxt.mode == "full" and nxt.reason != "first":
        ctx.log.warning(
            "replica_full_fallback reason=%s",
            nxt.reason,
            extra={"fields": {"event": "replica_full_fallback", "vm_id": ctx.target_id, "reason": nxt.reason}},
        )


def backup(ctx: StepContext) -> None:
    row = _need_row(ctx)
    if row.status == "succeeded":
        return
    vm = _vm(ctx)
    uri = _uri(ctx, row.host_id)
    # Leftovers of an interrupted attempt: a running job, this replica's checkpoint, a half-written target.
    stop_backup_job(ctx.driver, uri, vm.name)
    if row.checkpoint in ctx.driver.list_checkpoints(uri, vm.name):
        ctx.driver.delete_checkpoint(uri, vm.name, row.checkpoint)
    if ctx.driver.disk_exists(row.dr_path):
        ctx.driver.delete_disk(row.dr_path)
    if row.mode == "full":
        ctx.driver.create_blank_disk(row.dr_path, like=vm.disk_path)
    else:
        ctx.driver.create_disk_overlay(row.backing_path, row.dr_path)
    with ctx.session_factory() as s, s.begin():
        s.get(Replica, row.id).snapshot_time = datetime.now(UTC)
    result = ctx.driver.backup_domain(uri, vm.name, row.dr_path, row.checkpoint, row.since_checkpoint)
    with ctx.session_factory() as s, s.begin():
        s.get(Replica, row.id).duration_ms = result.duration_ms


def undo_backup(ctx: StepContext) -> None:
    row = _row(ctx)
    if row is None or row.status != "running":
        return  # a succeeded replica (a later step failed) must keep its file and checkpoint
    vm = _vm(ctx)
    # Each domain part may fail (domain gone, host down, job still unwinding); none of that may keep the file here.
    try:
        uri = _uri(ctx, row.host_id)
        try:
            stop_backup_job(ctx.driver, uri, vm.name)
        except (DriverError, RuntimeError) as e:
            ctx.log.warning("undo_backup: could not stop the backup job: %s", e)
        if row.checkpoint.startswith(CHECKPOINT_PREFIX) and row.checkpoint in ctx.driver.list_checkpoints(uri, vm.name):
            ctx.driver.delete_checkpoint(uri, vm.name, row.checkpoint)
    except (DomainNotFound, HostUnreachable):
        pass
    except DriverError as e:
        ctx.log.warning("undo_backup: could not drop checkpoint %s: %s", row.checkpoint, e)
    if row.dr_path and ctx.driver.disk_exists(row.dr_path):
        ctx.driver.delete_disk(row.dr_path)


def record(ctx: StepContext) -> None:
    row = _need_row(ctx)
    if row.status == "succeeded":
        return
    size = ctx.driver.disk_allocation(row.dr_path)
    with ctx.session_factory() as s, s.begin():
        r = s.get(Replica, row.id)
        r.size_bytes = size
        # the worker clock, like snapshot_time in `backup`: achieved_rpo_s subtracts the two, so DB/worker skew
        # must not leak into it
        r.finished_at = datetime.now(UTC)
        r.status = "succeeded"
        mode, duration = r.mode, r.duration_ms
    ctx.log.info(
        "replica_succeeded",
        extra={
            "fields": {
                "event": "replica_succeeded",
                "vm_id": ctx.target_id,
                "replica_id": row.id,
                "mode": mode,
                "size_bytes": size,
                "duration_ms": duration,
            }
        },
    )


def trim_checkpoints(ctx: StepContext) -> None:
    row = _need_row(ctx)
    vm = _vm(ctx)
    delete_minihci_checkpoints(ctx.driver, _uri(ctx, row.host_id), vm.name, keep=row.checkpoint)


def _warn_undeletable(ctx: StepContext, rid: int, path: str | None, err: Exception) -> None:
    ctx.log.warning(
        "replica_file_delete_failed replica_id=%s path=%s: %s",
        rid,
        path,
        err,
        extra={
            "fields": {
                "event": "replica_file_delete_failed",
                "vm_id": ctx.target_id,
                "replica_id": rid,
                "path": path,
            }
        },
    )


def sweep_dead_rows(ctx: StepContext) -> None:
    """Converge leftovers of attempts whose undo did not finish: files of `failed` rows, and of `running` rows whose
    task is no longer open. Never touches a running row whose task is still open."""
    with ctx.session_factory() as s:
        dead = [
            (r.id, r.status, r.dr_path)
            for r in s.scalars(
                select(Replica)
                .join(Task, Task.id == Replica.task_id)
                .where(
                    Replica.vm_id == ctx.target_id,
                    (Replica.status == "failed")
                    | ((Replica.status == "running") & Task.state.in_(("failed", "rolled_back"))),
                )
            )
        ]
    for rid, status, path in dead:
        try:
            if path and ctx.driver.disk_exists(path):
                ctx.driver.delete_disk(path)
        except DriverError as e:
            _warn_undeletable(ctx, rid, path, e)
            continue  # leave the row as it is; the next run retries
        if status == "running":
            with ctx.session_factory() as s, s.begin():
                row = s.get(Replica, rid)
                if row.status == "running":
                    row.status = "failed"
                    row.finished_at = datetime.now(UTC)
            ctx.log.info(
                "replica_swept",
                extra={"fields": {"event": "replica_swept", "vm_id": ctx.target_id, "replica_id": rid}},
            )


def prune(ctx: StepContext) -> None:
    sweep_dead_rows(ctx)
    with ctx.session_factory() as s:
        policy = s.get(ProtectionPolicy, ctx.target_id)
        retention = policy.retention if policy else 1
        infos = [_info(r) for r in _succeeded(s, ctx.target_id)]
    paths = {r.id: r.dr_path for r in infos}
    chain = {r.id: (r.chain_id, r.seq) for r in infos}
    stuck: dict[int, int] = {}  # chain_id -> seq of a row whose file could not be deleted
    for rid in prune_ids(infos, retention):
        chain_id, seq = chain[rid]
        if chain_id in stuck and seq < stuck[chain_id]:
            continue  # never delete a backing file while a child that needs it still exists
        try:
            if ctx.driver.disk_exists(paths[rid]):
                ctx.driver.delete_disk(paths[rid])
        except DriverError as e:
            stuck[chain_id] = seq
            _warn_undeletable(ctx, rid, paths[rid], e)
            continue
        with ctx.session_factory() as s, s.begin():
            s.get(Replica, rid).status = "pruned"
        ctx.log.info(
            "replica_pruned",
            extra={"fields": {"event": "replica_pruned", "vm_id": ctx.target_id, "replica_id": rid}},
        )


def mark_failed(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        row = s.scalars(select(Replica).where(Replica.task_id == ctx.task_id)).one_or_none()
        if row is not None and row.status == "running":
            row.status = "failed"
            row.finished_at = datetime.now(UTC)


VM_REPLICATE = register(
    TaskType(
        "vm.replicate",
        "vm",
        [
            Step("plan", plan),
            Step("backup", backup, undo_backup),
            Step("record", record),
            Step("trim_checkpoints", trim_checkpoints),
            Step("prune", prune),
        ],
        mark_failed,
    )
)
