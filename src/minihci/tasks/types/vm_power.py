import time

from minihci.driver.base import DomainState
from minihci.models import Host, Vm
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register
from minihci.vmstate import apply_status

POLL_INTERVAL_S = 0.2


def _load(ctx: StepContext) -> tuple[str, str]:
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        return vm.name, s.get(Host, vm.host_id).libvirt_uri


def _set_actual(ctx: StepContext, value: str) -> None:
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        vm.actual_state, vm.state_reason = value, None


def _ensure_started(ctx: StepContext) -> None:
    name, uri = _load(ctx)
    state = ctx.driver.domain_state(uri, name)
    if state is None:
        raise RuntimeError("domain not defined")
    if state == DomainState.PAUSED:
        ctx.driver.resume_domain(uri, name)  # a paused guest keeps its memory; start would fail on it
    elif state == DomainState.CRASHED:
        ctx.driver.destroy_domain(uri, name)  # still active: stop it, then boot it again
        ctx.driver.start_domain(uri, name)
    elif state != DomainState.RUNNING:
        ctx.driver.start_domain(uri, name)


def start(ctx: StepContext) -> None:
    _ensure_started(ctx)


def stop(ctx: StepContext) -> None:
    name, uri = _load(ctx)
    state = ctx.driver.domain_state(uri, name)
    if state is None:
        raise RuntimeError("domain not defined")
    if not state.active:
        return
    if ctx.params.get("force") or state != DomainState.RUNNING:
        # a paused or crashed guest cannot act on an ACPI shutdown request
        ctx.driver.destroy_domain(uri, name)
        return
    ctx.driver.shutdown_domain(uri, name)
    deadline = time.monotonic() + ctx.settings.vm_shutdown_timeout_s
    while (st := ctx.driver.domain_state(uri, name)) is not None and st.active:
        if time.monotonic() >= deadline:
            raise RuntimeError("graceful shutdown timed out; retry with force")
        time.sleep(POLL_INTERVAL_S)


def reboot(ctx: StepContext) -> None:
    name, uri = _load(ctx)
    state = ctx.driver.domain_state(uri, name)
    if state == DomainState.RUNNING:
        ctx.driver.reboot_domain(uri, name)
    else:
        _ensure_started(ctx)


def mark_running(ctx: StepContext) -> None:
    _set_actual(ctx, "running")


def mark_stopped(ctx: StepContext) -> None:
    _set_actual(ctx, "stopped")


def sync_actual_state(ctx: StepContext) -> None:
    """No undos (D-017): copy the driver's real state into actual_state. Never touches an absent row."""
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        if vm.actual_state == "absent":
            return
        name, uri = vm.name, s.get(Host, vm.host_id).libvirt_uri
    status = ctx.driver.domain_status(uri, name)
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        if status is None:
            vm.actual_state, vm.state_reason = "error", None
        else:
            apply_status(vm, status)


VM_START = register(
    TaskType("vm.start", "vm", [Step("start_domain", start), Step("mark_running", mark_running)], sync_actual_state)
)
VM_STOP = register(
    TaskType("vm.stop", "vm", [Step("stop_domain", stop), Step("mark_stopped", mark_stopped)], sync_actual_state)
)
VM_REBOOT = register(
    TaskType("vm.reboot", "vm", [Step("reboot_domain", reboot), Step("mark_running", mark_running)], sync_actual_state)
)
