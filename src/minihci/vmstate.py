"""How a driver-reported domain status becomes a VM's actual_state (D-026 amendment 4)."""

from minihci.driver.base import DomainState, DomainStatus
from minihci.models import Vm

DB_STATE = {
    DomainState.RUNNING: "running",
    DomainState.SHUT_OFF: "stopped",
    DomainState.PAUSED: "paused",
    DomainState.CRASHED: "crashed",
}

# A paused or crashed guest is not "stopped": start fails on it, and it is still holding its memory.
WEDGED = ("paused", "crashed")


def observed_state(status: DomainStatus | None) -> str:
    return "missing" if status is None else DB_STATE[status.state]


def keeps_reason(actual_state: str) -> bool:
    return actual_state in WEDGED


def apply_status(vm: Vm, status: DomainStatus | None) -> None:
    """Copy the observed state onto the row. The reason is kept only while the VM is paused or crashed."""
    vm.actual_state = observed_state(status)
    vm.state_reason = status.reason if status is not None and keeps_reason(vm.actual_state) else None


# Paused reasons the reconciler undoes by itself. Everything else is either a step of something still in
# progress (migration, save, dump, snapshot, starting up, shutting down), a guest that hung (watchdog), or
# needs an operator (ioerror), so resuming it would be wrong.
AUTO_RESUME_REASONS = ("user",)


def recoverable(actual_state: str, state_reason: str | None) -> bool:
    """Can `vm.start` bring a paused or crashed VM back without making things worse?"""
    if actual_state == "crashed":
        return True
    return actual_state == "paused" and state_reason in AUTO_RESUME_REASONS


def is_unhealthy(actual_state: str, state_reason: str | None) -> bool:
    return actual_state == "paused" and state_reason == "ioerror"
