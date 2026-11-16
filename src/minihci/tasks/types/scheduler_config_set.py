from sqlalchemy import text

from minihci.scheduler.config import CONFIG_FIELDS
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register


def apply(ctx: StepContext) -> None:
    """Upsert only the supplied keys (not UPDATE: the row may be absent; re-running is idempotent)."""
    values = {k: ctx.params[k] for k in CONFIG_FIELDS if k in ctx.params}
    cols = ", ".join(["id", *values, "updated_at"])
    vals = ", ".join(["1", *(f":{k}" for k in values), "now()"])
    sets = ", ".join([*(f"{k} = EXCLUDED.{k}" for k in values), "updated_at = now()"])
    with ctx.session_factory() as s, s.begin():
        s.execute(
            text(f"INSERT INTO scheduler_config ({cols}) VALUES ({vals}) ON CONFLICT (id) DO UPDATE SET {sets}"),
            values,
        )


SCHEDULER_CONFIG_SET = register(
    TaskType(name="scheduler.config_set", target_kind="scheduler", steps=[Step("apply", apply)])
)
