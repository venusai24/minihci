from minihci.models import Vm
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register
from minihci.tasks.types.vm_power import _load, sync_actual_state

# Forward-only (D-018): a deleted disk cannot be restored, so no step has an undo.
# Every step checks the driver first, so a re-run after a crash or a failure finishes the job.


def destroy_domain(ctx: StepContext) -> None:
    name, uri = _load(ctx)
    state = ctx.driver.domain_state(uri, name)
    if state is not None and state.active:  # also a paused or crashed domain: it must go before undefine
        ctx.driver.destroy_domain(uri, name)


def undefine_domain(ctx: StepContext) -> None:
    name, uri = _load(ctx)
    if ctx.driver.domain_state(uri, name) is not None:
        ctx.driver.undefine_domain(uri, name)


def delete_disk(ctx: StepContext) -> None:
    with ctx.session_factory() as s:
        path = s.get(Vm, ctx.target_id).disk_path
    if ctx.driver.disk_exists(path):
        ctx.driver.delete_disk(path)


def mark_absent(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        vm = s.get(Vm, ctx.target_id)
        vm.desired_state = vm.actual_state = "absent"


# sync_actual_state sets `error` when the domain is already gone, so a re-sent DELETE finishes it.
VM_DELETE = register(
    TaskType(
        "vm.delete",
        "vm",
        [
            Step("destroy_domain", destroy_domain),
            Step("undefine_domain", undefine_domain),
            Step("delete_disk", delete_disk),
            Step("mark_absent", mark_absent),
        ],
        sync_actual_state,
    )
)
