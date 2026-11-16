from sqlalchemy import text

from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register


def apply(ctx: StepContext) -> None:
    """Upsert (not UPDATE): the row may be absent, and re-running is idempotent."""
    with ctx.session_factory() as s, s.begin():
        s.execute(
            text(
                "INSERT INTO scheduler_config (id, cpu_headroom_pct, updated_at) VALUES (1, :pct, now()) "
                "ON CONFLICT (id) DO UPDATE SET cpu_headroom_pct = EXCLUDED.cpu_headroom_pct, updated_at = now()"
            ),
            {"pct": int(ctx.params["cpu_headroom_pct"])},
        )


SCHEDULER_CONFIG_SET = register(
    TaskType(name="scheduler.config_set", target_kind="scheduler", steps=[Step("apply", apply)])
)
