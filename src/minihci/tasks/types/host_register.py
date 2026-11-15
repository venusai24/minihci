from minihci.models import Host
from minihci.tasks.executor import Step, StepContext, TaskType
from minihci.tasks.registry import register


def probe(ctx: StepContext) -> None:
    """Naturally idempotent: re-probing just rewrites the same values."""
    with ctx.session_factory() as s:
        uri = s.get(Host, ctx.target_id).libvirt_uri
    info = ctx.driver.probe_host(uri)
    with ctx.session_factory() as s, s.begin():
        host = s.get(Host, ctx.target_id)
        host.cpu_total = info.cpu_total
        host.mem_total_mb = info.mem_total_mb
        host.status = "ready"


def mark_failed(ctx: StepContext) -> None:
    with ctx.session_factory() as s, s.begin():
        s.get(Host, ctx.target_id).status = "failed"


HOST_REGISTER = register(
    TaskType(name="host.register", target_kind="host", steps=[Step("probe", probe)], on_rolled_back=mark_failed)
)
