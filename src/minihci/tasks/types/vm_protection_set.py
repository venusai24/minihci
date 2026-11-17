from sqlalchemy import text

from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register


def apply(ctx: StepContext) -> None:
    """Upsert the policy row (idempotent: re-running leaves one row with the same values)."""
    values = {"vm_id": ctx.target_id, **{k: ctx.params[k] for k in ("interval_s", "retention", "enabled")}}
    with ctx.session_factory() as s, s.begin():
        s.execute(
            text(
                "INSERT INTO protection_policies (vm_id, interval_s, retention, enabled, updated_at) "
                "VALUES (:vm_id, :interval_s, :retention, :enabled, now()) "
                "ON CONFLICT (vm_id) DO UPDATE SET interval_s = EXCLUDED.interval_s, "
                "retention = EXCLUDED.retention, enabled = EXCLUDED.enabled, updated_at = now()"
            ),
            values,
        )


VM_PROTECTION_SET = register(TaskType(name="vm.protection_set", target_kind="vm", steps=[Step("apply", apply)]))
