"""One rebalancer pass (FR-3.4, D-025). Reads the DB only, never calls a driver; only enqueues vm.migrate."""

import logging
import threading

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from minihci.config import Settings
from minihci.models import Host, HostMetric, Task, Vm
from minihci.scheduler.config import load_config
from minihci.scheduler.placement import PLACEABLE_STATUSES, fits
from minihci.scheduler.rebalance import Move, choose_move
from minihci.tasks.engine import enqueue
from minihci.tasks.locks import LOCK_NAMESPACE

log = logging.getLogger("minihci.rebalancer")

MIN_SAMPLES = 2


def _migrate_in_flight(s: Session) -> bool:
    return (
        s.scalars(
            select(Task.id).where(Task.type == "vm.migrate", Task.state.in_(("pending", "running"))).limit(1)
        ).first()
        is not None
    )


def _usage(s: Session, exclude_vm: int | None = None) -> dict[int, tuple[int, int]]:
    q = (
        select(Vm.host_id, func.sum(Vm.vcpus), func.sum(Vm.mem_mb))
        .where(Vm.desired_state != "absent")
        .group_by(Vm.host_id)
    )
    if exclude_vm is not None:
        q = q.where(Vm.id != exclude_vm)
    return {hid: (int(c), int(m)) for hid, c, m in s.execute(q)}


def rebalance_once(session_factory: sessionmaker[Session], settings: Settings) -> Move | None:
    with session_factory() as s:
        config = load_config(s)
        if not config.rebalance_enabled or _migrate_in_flight(s):
            return None
        hosts = list(s.scalars(select(Host).where(Host.status.in_(PLACEABLE_STATUSES)).order_by(Host.id)))
        avg_cpu = {
            hid: float(avg)
            for hid, avg, n in s.execute(
                select(HostMetric.host_id, func.avg(HostMetric.cpu_pct), func.count())
                .where(
                    HostMetric.collected_at
                    >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, config.rebalance_window_s)
                )
                .group_by(HostMetric.host_id)
            )
            if n >= MIN_SAMPLES
        }
        vms = list(
            s.scalars(select(Vm).where(Vm.desired_state == "running", Vm.actual_state == "running").order_by(Vm.id))
        )
        usage = _usage(s)
        cooling_vms = set(
            s.scalars(
                select(Task.target_id).where(
                    Task.type == "vm.migrate",
                    Task.created_at
                    >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, config.rebalance_vm_cooldown_s),
                )
            )
        )
        cooling_hosts: set[int] = set()
        for params in s.scalars(
            select(Task.params).where(
                Task.type == "vm.migrate",
                Task.created_at >= func.now() - func.make_interval(0, 0, 0, 0, 0, 0, config.rebalance_host_cooldown_s),
            )
        ):
            for key in ("src_host_id", "dst_host_id"):
                if params.get(key) is not None:
                    cooling_hosts.add(int(params[key]))
        move = choose_move(hosts, avg_cpu, vms, usage, config, cooling_vms, cooling_hosts)
        if move is None:
            return None

    with session_factory() as s, s.begin():
        # Lock order matches api/vms.py: VM row first, then the scheduler advisory lock (D-025).
        vm = s.scalars(select(Vm).where(Vm.id == move.vm_id).with_for_update()).first()
        if (
            vm is None
            or vm.host_id != move.src_host_id
            or vm.desired_state != "running"
            or vm.actual_state != "running"
        ):
            return None
        busy = s.scalars(
            select(Task.id).where(
                Task.type.in_(("vm.delete", "vm.migrate")),
                Task.target_id == vm.id,
                Task.state.in_(("pending", "running")),
            )
        ).first()
        if busy is not None:
            return None
        s.execute(text("SELECT pg_advisory_xact_lock(:ns, 0)"), {"ns": LOCK_NAMESPACE["scheduler"]})
        if _migrate_in_flight(s):
            return None
        dst = s.scalars(select(Host).where(Host.id == move.dst_host_id).with_for_update(read=True)).first()
        if dst is None or dst.status not in PLACEABLE_STATUSES:
            return None
        if not fits(vm, dst, _usage(s, exclude_vm=vm.id), load_config(s)):
            return None
        task = enqueue(
            s,
            "vm.migrate",
            vm.id,
            {"src_host_id": vm.host_id, "dst_host_id": dst.id, "reason": "rebalance"},
        )
        task_id = task.id
    log.info(
        "rebalance_enqueued",
        extra={
            "fields": {
                "event": "rebalance_enqueued",
                "task_id": task_id,
                "vm_id": move.vm_id,
                "src_host_id": move.src_host_id,
                "dst_host_id": move.dst_host_id,
            }
        },
    )
    return move


def run_forever(stop: threading.Event, session_factory: sessionmaker[Session], settings: Settings) -> None:
    while not stop.is_set():
        try:
            rebalance_once(session_factory, settings)
        except Exception:
            log.exception("rebalance pass failed")
        stop.wait(settings.rebalance_interval_s)
