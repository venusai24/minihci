from minihci.models import Task
from minihci.tasks.executor import Step, TaskType


def make_three_step_type(calls: list, fail_at: str | None = None, undo_fails: bool = False,
                         name: str = "test.three") -> TaskType:
    def do(step):
        def f(ctx):
            calls.append(("do", step))
            if step == fail_at:
                raise RuntimeError(f"boom in {step}")
        return f

    def undo(step):
        def f(ctx):
            calls.append(("undo", step))
            if undo_fails:
                raise RuntimeError(f"undo broke {step}")
        return f

    return TaskType(name=name, target_kind="vm", steps=[Step(n, do(n), undo(n)) for n in ("a", "b", "c")])


def step_rows(*pairs: tuple[str, str]) -> list[dict]:
    return [{"name": n, "status": st, "started_at": None, "finished_at": None, "error": None} for n, st in pairs]


def load_task(session_factory, task_id: int) -> Task:
    with session_factory() as s:
        return s.get(Task, task_id)


from minihci.config import Settings
from minihci.tasks.registry import REGISTRY
from minihci.tasks.worker import Worker


def drain(session_factory, driver, registry=None, settings=None) -> list[int]:
    w = Worker(session_factory, driver, settings or Settings(), registry=REGISTRY if registry is None else registry)
    done = []
    while (tid := w.run_once()) is not None:
        done.append(tid)
    return done
