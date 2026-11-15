"""Reconciler (D-019, D-020): compare the DB with what the hosts report. Never calls a mutating driver method."""

import logging
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from minihci.config import Settings
from minihci.driver.base import DomainState, DriverError, HostDriver, HostUnreachable
from minihci.logging import task_context
from minihci.models import Host, Task, Vm
from minihci.tasks.engine import enqueue
from minihci.tasks.locks import LOCK_NAMESPACE

log = logging.getLogger("minihci.reconcile")

_DB_STATE = {DomainState.RUNNING: "running", DomainState.SHUT_OFF: "stopped"}


@dataclass
class ReconcileReport:
    missing: list[int] = field(default_factory=list)
    actual_drift: list[int] = field(default_factory=list)
    unknown: list[tuple[int, str]] = field(default_factory=list)  # (host_id, domain name)
    skipped: list[int] = field(default_factory=list)
    corrective: list[int] = field(default_factory=list)  # task ids (Q04.3)


def reconcile_once(session_factory: sessionmaker[Session], driver: HostDriver, settings: Settings) -> ReconcileReport:
    report = ReconcileReport()
    with session_factory() as s:
        hosts = [(h.id, h.libvirt_uri) for h in s.scalars(select(Host).where(Host.status == "ready").order_by(Host.id))]
    for host_id, uri in hosts:
        try:
            domains = driver.list_domains(uri)
        except HostUnreachable as e:
            log.warning(
                "host unreachable", extra={"fields": {"event": "host_unreachable", "host_id": host_id, "error": str(e)}}
            )
            continue
        with session_factory() as s:
            vms = [
                (v.id, v.name)
                for v in s.scalars(
                    select(Vm).where(Vm.host_id == host_id, Vm.desired_state != "absent").order_by(Vm.id)
                )
            ]
        known = {name for _, name in vms}
        for name in sorted(set(domains) - known):
            report.unknown.append((host_id, name))
            log.warning(
                "unknown domain",
                extra={"fields": {"event": "unknown_domain", "host_id": host_id, "domain": name}},
            )
        for i, (vm_id, _) in enumerate(vms):
            try:
                _reconcile_vm(session_factory, driver, host_id, uri, vm_id, report)
            except HostUnreachable as e:
                log.warning(
                    "host unreachable",
                    extra={
                        "fields": {"event": "host_unreachable", "host_id": host_id, "vm_id": vm_id, "error": str(e)}
                    },
                )
                report.skipped.extend(v for v, _ in vms[i:])
                break
            except DriverError as e:
                log.warning(
                    "vm check failed",
                    extra={"fields": {"event": "vm_check_failed", "host_id": host_id, "vm_id": vm_id, "error": str(e)}},
                )
                report.skipped.append(vm_id)
    return report


def _reconcile_vm(session_factory, driver, host_id: int, uri: str, vm_id: int, report: ReconcileReport) -> None:
    with session_factory() as s, s.begin():
        got = s.execute(
            text("SELECT pg_try_advisory_xact_lock(:ns, :id)"), {"ns": LOCK_NAMESPACE["vm"], "id": vm_id}
        ).scalar()
        if not got:
            report.skipped.append(vm_id)
            return
        vm = s.scalars(select(Vm).where(Vm.id == vm_id).with_for_update()).one()
        if vm.desired_state == "absent":
            return
        busy = s.scalars(
            select(Task.id)
            .where(Task.type.like("vm.%"), Task.target_id == vm_id, Task.state.in_(["pending", "running"]))
            .limit(1)
        ).first()
        if busy is not None:
            report.skipped.append(vm_id)
            return
        state = driver.domain_state(uri, vm.name)  # fresh read, under the lock
        observed = _DB_STATE[state] if state is not None else "missing"
        if observed != vm.actual_state:
            _record_drift(vm, state, observed, host_id, vm_id, report)
        if state is not None and observed != vm.desired_state:
            # D-019: never act directly; enqueue in this txn, under the VM lock, so passes cannot duplicate it
            ttype, params = ("vm.start", {}) if vm.desired_state == "running" else ("vm.stop", {"force": False})
            task = enqueue(s, ttype, vm_id, params)
            report.corrective.append(task.id)
            with task_context(task.id):
                log.warning(
                    "corrective task enqueued",
                    extra={
                        "fields": {
                            "event": "corrective_task",
                            "task_id": task.id,
                            "task_type": ttype,
                            "host_id": host_id,
                            "vm_id": vm_id,
                            "desired": vm.desired_state,
                            "observed": observed,
                        }
                    },
                )


def _record_drift(vm, state, observed: str, host_id: int, vm_id: int, report: ReconcileReport) -> None:
    fields = {"host_id": host_id, "vm_id": vm_id, "expected": vm.actual_state, "observed": observed}
    if state is None:
        fields["observed"] = None
        fields["expected"] = vm.desired_state if vm.actual_state == "pending" else vm.actual_state
        log.warning("vm missing", extra={"fields": {"event": "vm_missing", **fields}})
        vm.actual_state = "missing"
        report.missing.append(vm_id)
    else:
        log.warning("actual state drift", extra={"fields": {"event": "actual_drift", **fields}})
        vm.actual_state = observed
        report.actual_drift.append(vm_id)
