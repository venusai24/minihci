from minihci.driver.base import DomainSpec, DomainState
from minihci.models import Host, Vm
from minihci.paths import resolve_image
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register


def _load(ctx: StepContext) -> tuple[Vm, str]:
    with ctx.session_factory() as s:
        vm = s.get(Vm, ctx.target_id)
        uri = s.get(Host, vm.host_id).libvirt_uri
        s.expunge(vm)
        return vm, uri


# Every do/undo checks the actual driver state first, so any step can be re-run after a crash.


def create_disk(ctx: StepContext) -> None:
    vm, _ = _load(ctx)
    image = resolve_image(vm.image, ctx.settings.image_root)  # ValueError fails the step -> rollback
    if not ctx.driver.disk_exists(vm.disk_path):
        ctx.driver.create_disk_overlay(image, vm.disk_path)


def undo_create_disk(ctx: StepContext) -> None:
    vm, _ = _load(ctx)
    if ctx.driver.disk_exists(vm.disk_path):
        ctx.driver.delete_disk(vm.disk_path)


def define_domain(ctx: StepContext) -> None:
    vm, uri = _load(ctx)
    if ctx.driver.domain_state(uri, vm.name) is None:
        ctx.driver.define_domain(
            uri, DomainSpec(name=vm.name, vcpus=vm.vcpus, mem_mb=vm.mem_mb, disk_path=vm.disk_path)
        )


def undo_define_domain(ctx: StepContext) -> None:
    vm, uri = _load(ctx)
    state = ctx.driver.domain_state(uri, vm.name)
    if state == DomainState.RUNNING:
        ctx.driver.destroy_domain(uri, vm.name)
    if state is not None:
        ctx.driver.undefine_domain(uri, vm.name)


def start_domain(ctx: StepContext) -> None:
    vm, uri = _load(ctx)
    if ctx.driver.domain_state(uri, vm.name) != DomainState.RUNNING:
        ctx.driver.start_domain(uri, vm.name)


def undo_start_domain(ctx: StepContext) -> None:
    vm, uri = _load(ctx)
    if ctx.driver.domain_state(uri, vm.name) == DomainState.RUNNING:
        ctx.driver.destroy_domain(uri, vm.name)


def mark_running(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        s.get(Vm, ctx.target_id).actual_state = "running"


def mark_absent(ctx: StepContext) -> None:
    """Rolled back: keep the row for audit, but record that nothing exists any more."""
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        vm.desired_state = vm.actual_state = "absent"


VM_CREATE = register(
    TaskType(
        name="vm.create",
        target_kind="vm",
        steps=[
            Step("create_disk", create_disk, undo_create_disk),
            Step("define_domain", define_domain, undo_define_domain),
            Step("start_domain", start_domain, undo_start_domain),
            Step("mark_running", mark_running),
        ],
        on_rolled_back=mark_absent,
    )
)
